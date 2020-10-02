# -*- coding: utf-8; -*-
'''Utilities for building REPLs.'''

__all__ = ["doc", "sourcecode"]

import inspect

def doc(obj):
    """Print an object's docstring, non-interactively.

    Additionally, if the information is available, print the filename
    and the starting line number of the definition of `obj` in that file.
    This is printed before the actual docstring.
    """
    if not hasattr(obj, "__doc__") or not obj.__doc__:
        print("<no docstring>")
        return
    try:
        filename = inspect.getsourcefile(obj)
        source, firstlineno = inspect.getsourcelines(obj)
        print(f"{filename}:{firstlineno}")
    except (TypeError, OSError):
        pass
    print(inspect.cleandoc(obj.__doc__))

def sourcecode(obj):
    """Print an object's source code, non-interactively.

    Additionally, if the information is available, print the filename
    and the starting line number of the definition of `obj` in that file.
    This is printed before the actual source code.
    """
    try:
        filename = inspect.getsourcefile(obj)
        source, firstlineno = inspect.getsourcelines(obj)
        print(f"{filename}:{firstlineno}")
        for line in source:
            print(line.rstrip("\n"))
    except (TypeError, OSError):
        print("<no source code available>")
