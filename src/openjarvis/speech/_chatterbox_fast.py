"""CUDA-graph decoding for Chatterbox-Turbo's speech-token model.

Stock Chatterbox decodes each speech token with an eager Hugging Face GPT-2
forward pass plus Python logits processors. At batch size 1 that is hundreds
of small kernel launches per token, so on a laptop (especially on battery)
the CPU sets the pace: ~40 ms per token, slower than real time.

Here the same weights (in bfloat16) decode against a fixed-size KV cache, and
one whole step - forward pass, temperature/top-k/top-p/repetition penalty and
sampling - is captured as a CUDA graph and replayed per token. The voice
prompt (~380 tokens, identical for every sentence) is run once per voice and
kept in the cache, so a sentence only prefills its own text. Anything
unexpected falls back to the stock decoder.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Replays between checks for the stop token (each check is a GPU sync).
_CHECK_EVERY = 4
# Attention spans with their own captured graph: short sentences don't pay
# for attending over the whole cache.
_WINDOWS = (768, 1280, 2048)


class SentenceTooLong(ValueError):
    """The sentence needs more cache than the graph decoder has.

    A ValueError (not RuntimeError) so it's never mistaken for a GPU fault:
    the TTS backend splits the text and keeps using the fast decoder.
    """


class GraphDecoder:
    """Replays one captured GPT-2 decoding + sampling step per speech token."""

    def __init__(
        self,
        t3: Any,
        *,
        max_len: int = 2048,
        temperature: float = 0.8,
        top_k: int = 1000,
        top_p: float = 0.95,
        repetition_penalty: float = 1.2,
    ) -> None:
        import torch

        tfmr = t3.tfmr
        cfg = tfmr.config
        self.t3 = t3
        self.blocks = list(tfmr.h)
        self.heads = cfg.n_head
        self.width = cfg.n_embd
        self.head_dim = cfg.n_embd // cfg.n_head
        self.max_len = min(max_len, cfg.n_positions)
        self.device = next(tfmr.parameters()).device
        self.dtype = next(tfmr.parameters()).dtype
        self.vocab = t3.speech_head.out_features
        self.stop = int(t3.hp.stop_speech_token)
        self.settings = (temperature, top_k, top_p, repetition_penalty)
        dev, dt = self.device, self.dtype
        shape = (len(self.blocks), 1, self.heads, self.max_len, self.head_dim)
        self.keys = torch.zeros(shape, device=dev, dtype=dt)
        self.values = torch.zeros(shape, device=dev, dtype=dt)
        self.arange = torch.arange(self.max_len, device=dev)
        # Static inputs/outputs of the captured step.
        self.pos = torch.zeros(1, dtype=torch.long, device=dev)
        self.token = torch.zeros(1, 1, dtype=torch.long, device=dev)
        self.count = torch.zeros(1, dtype=torch.long, device=dev)
        self.ids = torch.zeros(1, self.max_len, dtype=torch.long, device=dev)
        self.seen = torch.zeros(1, self.vocab, dtype=torch.bool, device=dev)
        self.graphs: dict = {}
        self.window = self.max_len
        self.prefix_cond: Optional[Any] = None
        self.prefix_parts: tuple = ()
        self.prefix_len = 0

    # -- transformer ------------------------------------------------------------

    def _blocks(self, x: Any, mask: Any, positions: Any) -> Any:
        import torch.nn.functional as F

        n = x.shape[1]
        span = mask.shape[-1]
        for i, block in enumerate(self.blocks):
            h = block.ln_1(x)
            q, k, v = block.attn.c_attn(h).split(self.width, dim=2)
            q = q.view(1, n, self.heads, self.head_dim).transpose(1, 2)
            k = k.view(1, n, self.heads, self.head_dim).transpose(1, 2)
            v = v.view(1, n, self.heads, self.head_dim).transpose(1, 2)
            self.keys[i].index_copy_(2, positions, k)
            self.values[i].index_copy_(2, positions, v)
            a = F.scaled_dot_product_attention(
                q,
                self.keys[i][:, :, :span],
                self.values[i][:, :, :span],
                attn_mask=mask,
            )
            a = a.transpose(1, 2).reshape(1, n, self.width)
            x = x + block.attn.c_proj(a)
            x = x + block.mlp(block.ln_2(x))
        return self.t3.tfmr.ln_f(x)

    def _prefill(self, embeds: Any, start: int) -> Any:
        """Run ``embeds`` at positions start.. (eager); returns logits of the last."""
        import torch

        n = embeds.shape[1]
        positions = torch.arange(start, start + n, device=self.device)
        mask = self.arange.view(1, -1) <= positions.view(-1, 1)  # (n, max_len)
        x = embeds.to(self.dtype) + self.t3.tfmr.wpe(positions).unsqueeze(0)
        hidden = self._blocks(x, mask.view(1, 1, n, self.max_len), positions)
        return self.t3.speech_head(hidden[:, -1:].float())[:, -1, :]

    def _sample(self, logits: Any, seen: Any) -> Any:
        """HF's Temperature -> TopK -> TopP -> RepetitionPenalty, then sample."""
        import torch

        temperature, top_k, top_p, penalty = self.settings
        neg = torch.finfo(logits.dtype).min
        scores = logits / temperature if temperature > 0 else logits
        if 0 < top_k < self.vocab:
            kth = torch.topk(scores, top_k, dim=-1).values[..., -1:]
            scores = scores.masked_fill(scores < kth, neg)
        if top_p < 1.0:
            ordered, order = torch.sort(scores, descending=False)
            cumulative = ordered.softmax(dim=-1).cumsum(dim=-1)
            drop = cumulative <= (1 - top_p)
            drop[..., -1:] = False
            scores = scores.masked_fill(drop.scatter(1, order, drop), neg)
        if penalty != 1.0:
            penalised = torch.where(scores < 0, scores * penalty, scores / penalty)
            scores = torch.where(seen, penalised, scores)
        # Gumbel-max: the same distribution as multinomial(softmax(scores)),
        # with no host sync, so it can live inside the graph.
        uniform = torch.rand_like(scores).clamp_(1e-10, 0.9999999)
        noise = -torch.log(-torch.log(uniform))
        return torch.argmax(scores + noise, dim=-1, keepdim=True)

    def _step(self) -> None:
        x = self.t3.speech_emb(self.token).to(self.dtype)
        x = x + self.t3.tfmr.wpe(self.pos).unsqueeze(0)
        span = self.window
        mask = (self.arange[:span] <= self.pos).view(1, 1, 1, span)
        hidden = self._blocks(x, mask, self.pos)
        logits = self.t3.speech_head(hidden[:, -1:].float())[:, -1, :]
        token = self._sample(logits, self.seen)
        self.token.copy_(token)
        self.ids.index_copy_(1, self.count, token)
        self.seen.scatter_(1, token, True)
        self.pos.add_(1)
        self.count.add_(1)

    def _capture(self, window: int) -> Any:
        import torch

        self.window = window
        saved = (self.pos.clone(), self.count.clone(), self.seen.clone())
        saved_tok = self.token.clone()
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            for _ in range(2):  # warm-up runs required before capture
                self._step()
        torch.cuda.current_stream().wait_stream(stream)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            self._step()
        self.pos.copy_(saved[0])
        self.count.copy_(saved[1])
        self.seen.copy_(saved[2])
        self.token.copy_(saved_tok)
        self.graphs[window] = graph
        return graph

    # -- generation -------------------------------------------------------------

    def _ensure_prefix(self, t3_cond: Any) -> None:
        parts = (
            getattr(t3_cond, "speaker_emb", None),
            getattr(t3_cond, "cond_prompt_speech_tokens", None),
        )
        if t3_cond is self.prefix_cond and all(
            a is b for a, b in zip(parts, self.prefix_parts)
        ):
            return
        cond = self.t3.prepare_conditioning(t3_cond)  # (1, len_cond, dim)
        if cond.shape[1] >= self.max_len // 2:
            raise RuntimeError("voice prompt too long for the graph decoder")
        self._prefill(cond, 0)
        # Hold references: an id() could be reused by a later voice.
        self.prefix_cond, self.prefix_parts = t3_cond, parts
        self.prefix_len = cond.shape[1]

    def _expected(self, text_tokens: Any) -> int:
        """Cache positions a sentence needs (speech ~3 tokens per text token)."""
        return self.prefix_len + text_tokens.shape[1] * 5 + 80

    def fits(self, text_tokens: Any) -> bool:
        return self._expected(text_tokens) < self.max_len

    def generate(self, t3_cond: Any, text_tokens: Any, max_gen_len: int = 1000) -> Any:
        """Same contract as ``T3.inference_turbo``: speech tokens (1, n)."""
        import torch

        t3 = self.t3
        self._ensure_prefix(t3_cond)
        start = t3.hp.start_speech_token * torch.ones_like(text_tokens[:, :1])
        text = torch.cat([t3.text_emb(text_tokens), t3.speech_emb(start)], dim=1)
        begin = self.prefix_len + text.shape[1]
        need = self._expected(text_tokens)
        window = next((w for w in _WINDOWS if need < w <= self.max_len), self.max_len)
        budget = min(max_gen_len, window - begin)
        logits = self._prefill(text, self.prefix_len)
        start_seen = torch.zeros_like(self.seen)
        start_seen[0, int(start)] = True
        first = self._sample(logits, start_seen)  # stock: input_ids = [start]
        self.ids.zero_()
        self.ids[:, :1] = first
        self.seen.zero_()
        self.seen.scatter_(1, first, True)
        self.token.copy_(first)
        self.pos.fill_(begin)
        self.count.fill_(1)
        graph = self.graphs.get(window) or self._capture(window)
        done = int(first) == self.stop
        steps = 1
        while not done and steps < budget:
            for _ in range(min(_CHECK_EVERY, budget - steps)):
                graph.replay()
                steps += 1
            recent = self.ids[0, max(0, steps - _CHECK_EVERY - 1) : steps]
            done = bool((recent == self.stop).any())
        tokens = self.ids[:, :steps]
        hits = (tokens[0] == self.stop).nonzero()
        if hits.numel():
            tokens = tokens[:, : int(hits[0])]
        elif steps >= budget:
            raise SentenceTooLong("sentence longer than the graph decoder's cache")
        return tokens.clone()


def install(model: Any, *, max_len: int = 2048) -> bool:
    """Route ``model.t3.inference_turbo`` through a GraphDecoder (CUDA only).

    The GPT-2 backbone moves to bfloat16 (half the memory traffic and VRAM).
    Returns False, leaving stock decoding, without CUDA or on failure.
    """
    try:
        import torch

        if not torch.cuda.is_available() or str(model.device).startswith("cpu"):
            return False
        t3 = model.t3
        if getattr(t3, "_openjarvis_graph", None) is not None:
            return True
        stock = t3.inference_turbo
        t3.tfmr.to(torch.bfloat16)
        decoder = GraphDecoder(t3, max_len=max_len)

        def back_to_stock() -> None:
            t3.tfmr.to(torch.float32)
            t3.inference_turbo = stock
            t3._openjarvis_graph = None

        def inference_turbo(t3_cond, text_tokens, **kwargs):
            try:
                with torch.inference_mode():
                    decoder._ensure_prefix(t3_cond)
                    if not decoder.fits(text_tokens):
                        raise SentenceTooLong("sentence too long for the cache")
                    return decoder.generate(t3_cond, text_tokens)
            except SentenceTooLong:
                # Not a fault: the caller splits the sentence and stays fast.
                raise
            except Exception as exc:  # pragma: no cover - depends on the GPU
                # The graphs point at the bfloat16 weights, so once the model
                # goes back to float32 they're never replayed again.
                logger.warning("Fast speech decoding failed (%s); using stock", exc)
                back_to_stock()
                return stock(t3_cond, text_tokens, **kwargs)

        t3.inference_turbo = inference_turbo
        t3._openjarvis_graph = decoder
        return True
    except Exception as exc:  # pragma: no cover - depends on the GPU
        logger.warning("Fast speech decoding unavailable: %s", exc)
        try:
            model.t3.tfmr.to(torch.float32)
        except Exception:
            pass
        return False


__all__ = ["GraphDecoder", "SentenceTooLong", "install"]
