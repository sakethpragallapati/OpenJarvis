"""Personal-computer integration: notifications, power, reminders, OS control.

Everything here is dependency-free (ctypes / stdlib / the OS's own tools) so
it works in a bare install. Windows is the primary target; macOS and Linux
get the same API where the OS offers an equivalent.
"""
