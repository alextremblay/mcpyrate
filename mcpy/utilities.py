# -*- coding: utf-8; -*-
'''General utilities. Can be useful for writing both macros as well as macro expanders.'''

__all__ = ['gensym', 'flatten_suite',
           'format_location', 'format_macrofunction',
           'NestingLevelTracker',
           'NodeVisitorListMixin', 'NodeTransformerListMixin']

from contextlib import contextmanager
import uuid


_previous_gensyms = set()
def gensym(basename=None):
    """Create a name for a new, unused lexical identifier, and return the name as an `str`.

    We include an uuid in the name to avoid the need for any lexical scanning.

    Can also be used for globally unique string keys, in which case `basename`
    does not need to be a valid identifier.
    """
    basename = basename or "gensym"
    def generate():
        unique = str(uuid.uuid4()).replace('-', '')
        return f"{basename}_{unique}"
    sym = generate()
    # The uuid spec does not guarantee no collisions, only a vanishingly small chance.
    while sym in _previous_gensyms:
        sym = generate()  # pragma: no cover
    _previous_gensyms.add(sym)
    return sym


def flatten_suite(lst):
    """Flatten a statement suite (by one level).

    `lst` may contain both individual items and `list`s. Any item that
    `is None` is omitted. If the final result is empty, then instead of
    an empty list, return `None`.
    """
    out = []
    for elt in lst:
        if isinstance(elt, list):
            out.extend(elt)
        elif elt is not None:
            out.append(elt)
    return out if out else None


def format_location(filename, tree, sourcecode):
    '''Format a source code location in a standard way, for error messages.

    `filename`: full path to `.py` file
    `tree`: AST node to get source line number from
    `sourcecode`: source code (typically, to get this, `unparse(tree)`
                  before expanding it), or `None` to omit it.
    '''
    lineno = tree.lineno if hasattr(tree, 'lineno') else None
    if sourcecode:
        sep = " " if "\n" not in sourcecode else "\n"
        source_with_sep = f"{sep}{sourcecode}"
    else:
        source_with_sep = ""
    return f'{filename}:{lineno}:{source_with_sep}'


def format_macrofunction(function):
    '''Format the fully qualified name of a macro function, for error messages.'''
    return f"{function.__module__}.{function.__qualname__}"


class NestingLevelTracker:
    """Track the nesting level in a set of co-operating, related macros.

    Useful for implementing macros that are syntactically only valid inside the
    invocation of another macro (i.e. when the level is `> 0`).
    """
    def __init__(self, start=0):
        """start: int, initial level"""
        self.stack = [start]

    def _get_value(self):
        return self.stack[-1]
    value = property(fget=_get_value, doc="The current level. Read-only. Use `set_to` or `change_by` to change.")

    def set_to(self, value):
        """Context manager. Run a section of code with the level set to `value`."""
        if not isinstance(value, int):
            raise TypeError(f"Expected integer `value`, got {type(value)} with value {repr(value)}")
        if value < 0:
            raise ValueError(f"`value` must be >= 0, got {repr(value)}")
        @contextmanager
        def _set_to():
            self.stack.append(value)
            try:
                yield
            finally:
                self.stack.pop()
                assert self.stack  # postcondition
        return _set_to()

    def changed_by(self, delta):
        """Context manager. Run a section of code with the level incremented by `delta`."""
        return self.set_to(self.value + delta)


class NodeVisitorListMixin:
    """Mixin for `ast.NodeVisitor`.

    Make `visit()` automatically walk lists of AST nodes, and no-op on `None`.
    """
    def visit(self, tree):
        if tree is None:
            return
        if isinstance(tree, list):
            for elt in tree:
                self.visit(elt)
            return
        super().visit(tree)


class NodeTransformerListMixin:
    """Mixin for `ast.NodeTransformer`.

    Make `visit()` automatically walk lists of AST nodes, and no-op on `None`.
    """
    def visit(self, tree):
        if tree is None:
            return None
        if isinstance(tree, list):
            return flatten_suite(self.visit(elt) for elt in tree)
        return super().visit(tree)
