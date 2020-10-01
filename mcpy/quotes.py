# -*- coding: utf-8; -*-
"""Quasiquotes. Build ASTs in your macros, using syntax that mostly looks like regular code."""

__all__ = ['capture', 'lookup', 'astify', 'unastify',
           'q', 'u', 'n', 'a', 's', 'h',
           'expand1q', 'expandq',
           'expand1', 'expand']

import ast
from .core import _hygienic_bindings
from .expander import expand_macros
from .markers import ASTMarker, get_markers
from .unparser import unparse
from .utilities import gensym, NestingLevelTracker

# --------------------------------------------------------------------------------

class QuasiquoteMarker(ASTMarker):
    """Base class for AST markers used by quasiquotes. Compiled away by `astify`."""
    pass

class ASTLiteral(QuasiquoteMarker):  # like MacroPy's `Literal`
    """Keep the given subtree as-is."""
    pass

class CaptureLater(QuasiquoteMarker):  # like MacroPy's `Captured`
    """Capture the value the given subtree evaluates to at the use site of `q`."""
    def __init__(self, body, name):
        super().__init__(body)
        self.name = name
        self._fields += ["name"]

# --------------------------------------------------------------------------------

# Hygienically captured run-time values.
_hygienic_registry = {}

def _mcpy_quotes_attr(attr):
    """Create an AST that, when compiled and run, looks up `mcpy.quotes.attr` in `Load` context."""
    mcpy_quotes_module = ast.Attribute(value=ast.Name(id="mcpy", ctx=ast.Load()),
                                       attr="quotes",
                                       ctx=ast.Load())
    return ast.Attribute(value=mcpy_quotes_module,
                         attr=attr,
                         ctx=ast.Load())

def _capture_into(mapping, value, basename):
    for k, v in mapping.items():
        if v is value:
            key = k
            break
    else:
        key = gensym(basename)
        mapping[key] = value
    return key

def capture(value, basename):
    """Store value into the hygienic capture registry.

    `value`:    Any run-time value.
    `basename`: Basename for gensymming a unique key for the value.
                Does not need to be an identifier.

    The return value is an AST that, when compiled and run, looks up the
    captured value. Each unique value (by `id`) is only stored once.

    Hygienically captured macros are treated using a different mechanism;
    see `mcpy.core._hygienic_bindings`.
    """
    key = _capture_into(_hygienic_registry, value, basename)
    return ast.Call(_mcpy_quotes_attr("lookup"),
                    [ast.Constant(value=key)],
                    [])

def lookup(key):
    """Look up a hygienically captured run-time value."""
    return _hygienic_registry[key]

# --------------------------------------------------------------------------------

def astify(x, expander=None):  # like MacroPy's `ast_repr`
    """Lift a value into its AST representation, if possible.

    When the AST is compiled and run, it will evaluate to `x`.

    If `x` itself is an AST, then produce an AST that, when compiled and run,
    will generate the AST `x`.

    If the input is a `list` of ASTs for a statement suite, the return value
    is a single `ast.List` node, with its `elts` taken from the input list.
    However, most of the time it's not used this way, because `BaseMacroExpander`
    already translates a `visit` to a statement suite into visits to individual
    nodes, because otherwise `ast.NodeTransformer` chokes on the input. (The only
    exception is `q` in block mode; it'll produce a `List` this way.)

    `expander` is a `BaseMacroExpander` instance, used for detecting macro names
    inside `CaptureLater` markers. If no `expander` is provided, macros cannot be
    hygienically captured.

    Raises `TypeError` when the lifting fails.
    """
    def recurse(x):  # second layer just to auto-pass `expander` by closure.
        T = type(x)

        # Drop the ASTLiteral wrapper; it only tells us to pass through this subtree as-is.
        if T is ASTLiteral:
            return x.body

        # This is the magic part of q[h[]].
        elif T is CaptureLater:
            if expander and type(x.body) is ast.Name and expander.ismacroname(x.body.id):
                # Hygienically capture a macro name. We do this immediately,
                # during the expansion of `q`. This allows macros in scope at
                # the use site of `q` to be hygienically propagated out to the
                # use site of the macro that used `q`. So you can write macros
                # that `q[h[macroname][...]]` and `macroname` doesn't have to be
                # macro-imported wherever that code gets spliced in.
                macroname = x.body.id
                function = expander.bindings[macroname]
                uniquename = _capture_into(_hygienic_bindings, function, macroname)
                return recurse(ast.Name(id=uniquename))
            # Hygienically capture a garden variety run-time value.
            # At the use site of q[], this captures the value, and rewrites itself
            # into a lookup. At the use site of the macro that used q[], that
            # rewritten code looks up the captured value.
            return ast.Call(_mcpy_quotes_attr('capture'),
                            [x.body,
                             ast.Constant(value=x.name)],
                            [])

        elif T in (int, float, str, bytes, bool, type(None)):
            return ast.Constant(value=x)

        elif T is list:
            return ast.List(elts=list(recurse(elt) for elt in x))
        elif T is tuple:
            return ast.Tuple(elts=list(recurse(elt) for elt in x))
        elif T is dict:
            return ast.Dict(keys=list(recurse(k) for k in x.keys()),
                            values=list(recurse(v) for v in x.values()))
        elif T is set:
            return ast.Set(elts=list(recurse(elt) for elt in x))

        elif isinstance(x, ast.AST):
            # The magic is in the Call. Take apart the input AST, and construct a
            # new AST, that (when compiled and run) will re-generate the input AST.
            #
            # We refer to the stdlib `ast` module as `mcpy.quotes.ast` to avoid
            # name conflicts at the use site of `q[]`.
            fields = [ast.keyword(a, recurse(b)) for a, b in ast.iter_fields(x)]
            return ast.Call(ast.Attribute(value=_mcpy_quotes_attr('ast'),
                                          attr=x.__class__.__name__,
                                          ctx=ast.Load()),
                            [],
                            fields)

        raise TypeError(f"Don't know how to astify {repr(x)}")
    return recurse(x)

def unastify(tree):
    """Inverse of `astify`.

    `tree` must have been produced by `astify`. Otherwise raises `TypeError`.

    Essentially, this turns an AST representing quoted code back into an AST
    that represents that code directly, not quoted. So in a sense, `unastify`
    is a top-level unquote operator.

    Note subtle difference in meaning to `u[]`. The `u[]` operator interpolates
    a value from outside the quote context into the quoted representation - so
    that the value actually becomes quoted! - whereas `unastify` inverts the
    quote operation.
    """
    # CAUTION: in `unastify`, we implement only what we minimally need.
    def attr_ast_to_dotted_name(tree):
        # Input is like:
        #     (mcpy.quotes).thing
        #     ((mcpy.quotes).ast).thing
        if type(tree) is not ast.Attribute:
            raise TypeError
        acc = []
        def recurse(tree):
            acc.append(tree.attr)
            if type(tree.value) is ast.Attribute:
                recurse(tree.value)
            elif type(tree.value) is ast.Name:
                acc.append(tree.value.id)
            else:
                raise NotImplementedError
        recurse(tree)
        return ".".join(reversed(acc))

    our_module_globals = globals()
    def lookup_thing(dotted_name):
        if not dotted_name.startswith("mcpy.quotes"):
            raise NotImplementedError
        path = dotted_name.split(".")
        if len(path) < 3:
            raise NotImplementedError
        name_of_thing = path[2]
        thing = our_module_globals[name_of_thing]
        if len(path) > 3:
            for attrname in path[3:]:
                thing = getattr(thing, attrname)
        return thing

    T = type(tree)

    if T is ast.Constant:
        return tree.value

    # Support machinery for `Call` AST node. This serendipitously supports also
    # *args and **kwargs, because as of Python 3.6 those appear in `args` and
    # `keywords`, and `Starred` needs no special support here.
    elif T is list:
        return [unastify(elt) for elt in tree]
    elif T is ast.keyword:
        return tree.arg, unastify(tree.value)

    elif T is ast.List:
        return [unastify(elt) for elt in tree.elts]
    elif T is ast.Tuple:
        return tuple(unastify(elt) for elt in tree.elts)
    elif T is ast.Dict:
        return {unastify(k): unastify(v) for k, v in zip(tree.keys, tree.values)}
    elif T is ast.Set:
        return {unastify(elt) for elt in tree.elts}

    elif T is ast.Call:
        dotted_name = attr_ast_to_dotted_name(tree.func)
        callee = lookup_thing(dotted_name)
        args = unastify(tree.args)
        kwargs = {k: v for k, v in unastify(tree.keywords)}
        return callee(*args, **kwargs)

    raise TypeError(f"Don't know how to unastify {unparse(tree)}")

# --------------------------------------------------------------------------------
# Quasiquote macros
#
# These operators are named after Qu'nash, the goddess of quasiquotes in high-tech-elven mythology.

# TODO: Support nested quasiquotes properly, like Lisps do.
# (For higher-order macros that generate macros parametrically. Though that's not very useful
# until we can call other macros from the same module. We can, as a function, but the syntax...)

_quotelevel = NestingLevelTracker()

def _unquote_expand(tree, expander):
    """Expand quasiquote macros in `tree`. If quotelevel is zero, expand all macros in `tree`."""
    if _quotelevel.value == 0:
        tree = expander.visit_recursively(tree)  # result should be runnable, so always use recursive mode.
    else:
        tree = _expand_quasiquotes(tree, expander)

def _expand_quasiquotes(tree, expander):
    """Expand quasiquote macros only."""
    # Use a second expander instance, with different bindings. Copy only the
    # bindings of the quasiquote macros from the main `expander`, accounting
    # for possible as-imports. This second expander won't even see other macros,
    # thus leaving them alone.
    bindings = {k: v for k, v in expander.bindings.items() if v in (q, u, n, a, s, h)}
    return expand_macros(tree, bindings, expander.filename)

def q(tree, *, syntax, expander, **kw):
    """[syntax, expr/block] quasiquote. Lift code into its AST representation."""
    if syntax not in ("expr", "block"):
        raise SyntaxError("q is an expr and block macro only")
    with _quotelevel.changed_by(+1):
        tree = _expand_quasiquotes(tree, expander)  # expand any inner quotes and unquotes first
        tree = astify(tree, expander=expander)  # Magic part of `q`. Supply `expander` for `h[macro]` detection.
        ps = get_markers(tree, QuasiquoteMarker)  # postcondition: no remaining QuasiquoteMarkers
        if ps:
            assert False, f"QuasiquoteMarker instances remaining in output: {ps}"
        if syntax == 'block':
            target = kw['optional_vars']  # List, Tuple, Name
            if type(target) is not ast.Name:
                raise SyntaxError(f"expected a single asname, got {unparse(target)}")
            # Note this `Assign` runs at the use site of `q`, it's not part of the quoted code section.
            tree = ast.Assign([target], tree)  # Here `tree` is a List.
        return tree

def u(tree, *, syntax, expander, **kw):
    """[syntax, expr] unquote. Splice a simple value into a quasiquote.

    The value is lifted into an AST that re-constructs that value.
    """
    if syntax != "expr":
        raise SyntaxError("u is an expr macro only")
    if _quotelevel.value < 1:
        raise SyntaxError("u[] encountered while quotelevel < 1")
    with _quotelevel.changed_by(-1):
        _unquote_expand(tree, expander)
        # We want to generate an AST that compiles to the *value* of `v`. But when
        # this runs, it is too early. We must astify *at the use site*. So use an
        # `ast.Call` to delay, and in there, splice in `tree` as-is.
        return ASTLiteral(ast.Call(_mcpy_quotes_attr("astify"), [tree], []))

def n(tree, *, syntax, **kw):
    """[syntax, expr] name-unquote. Splice a string, lifted into a lexical identifier, into a quasiquote.

    The resulting node's `ctx` is filled in automatically by the macro expander later.
    """
    if syntax != "expr":
        raise SyntaxError("n is an expr macro only")
    if _quotelevel.value < 1:
        raise SyntaxError("n[] encountered while quotelevel < 1")
    with _quotelevel.changed_by(-1):
        return ASTLiteral(astify(ast.Name(id=ASTLiteral(tree))))

def a(tree, *, syntax, **kw):
    """[syntax, expr] AST-unquote. Splice an AST into a quasiquote."""
    if syntax != "expr":
        raise SyntaxError("a is an expr macro only")
    if _quotelevel.value < 1:
        raise SyntaxError("a[] encountered while quotelevel < 1")
    with _quotelevel.changed_by(-1):
        return ASTLiteral(tree)

def s(tree, *, syntax, **kw):
    """[syntax, expr] list-unquote. Splice a `list` of ASTs, as an `ast.List`, into a quasiquote."""
    if syntax != "expr":
        raise SyntaxError("s is an expr macro only")
    if _quotelevel.value < 1:
        raise SyntaxError("s[] encountered while quotelevel < 1")
    return ASTLiteral(ast.Call(ast.Attribute(value=_mcpy_quotes_attr('ast'),
                                             attr='List'),
                               [],
                               [ast.keyword("elts", tree)]))

def h(tree, *, syntax, expander, **kw):
    """[syntax, expr] hygienic-unquote. Splice any value, from the macro definition site, into a quasiquote.

    Supports also values that have no meaningful `repr`. The value is captured
    at the use site of the surrounding `q`.

    Supports also macros. To hygienically splice a macro invocation, `h[]` only
    the macro name.
    """
    if syntax != "expr":
        raise SyntaxError("h is an expr macro only")
    if _quotelevel.value < 1:
        raise SyntaxError("h[] encountered while quotelevel < 1")
    with _quotelevel.changed_by(-1):
        name = unparse(tree)
        _unquote_expand(tree, expander)
        return CaptureLater(tree, name)

# --------------------------------------------------------------------------------

def expand1q(tree, *, syntax, **kw):
    '''[syntax, expr/block] quote-then-expand-once.

    Quasiquote `tree`, then expand one layer of macros in it. Return the result
    quasiquoted.

    If your tree is already quasiquoted, use `expand1` instead.
    '''
    if syntax not in ("expr", "block"):
        raise SyntaxError("expand1q is an expr and block macro only")
    tree = q(tree, syntax=syntax, **kw)
    return expand1(tree, syntax=syntax, **kw)

def expandq(tree, *, syntax, **kw):
    '''[syntax, expr/block] quote-then-expand.

    Quasiquote `tree`, then expand it until no macros remain. Return the result
    quasiquoted. This operator is equivalent to MacroPy's `q`.

    If your tree is already quasiquoted, use `expand` instead.
    '''
    if syntax not in ("expr", "block"):
        raise SyntaxError("expandq is an expr and block macro only")
    tree = q(tree, syntax=syntax, **kw)
    return expand(tree, syntax=syntax, **kw)

# --------------------------------------------------------------------------------

def expand1(tree, *, syntax, expander, **kw):
    '''[syntax, expr/block] expand one layer of macros in quasiquoted `tree`.

    The result remains in quasiquoted form.

    Like calling `expander.visit_once(tree)`, but for quasiquoted `tree`.

    `tree` must be a quasiquoted AST; i.e. output from, or an invocation of,
    `q`, `expand1q`, `expandq`, `expand1`, or `expand`. Passing any other AST
    as `tree` raises `TypeError`.

    If your `tree` is not quasiquoted, `expand1q[...]` is a shorthand for
    `expand1[q[...]]`.
    '''
    if syntax not in ("expr", "block"):
        raise SyntaxError("expand1 is an expr and block macro only")
    # We first invert the quasiquote operation, then use the garden variety
    # `expander` on the result, and then re-quote the expanded AST.
    #
    # The first `visit_once` makes any quote invocations inside this macro invocation expand first.
    # If the input `tree` is an already expanded `q`, it will do nothing, because any macro invocations
    # are then in a quoted form, which don't look like macro invocations to the expander.
    # If the input `tree` is a `Done`, it will likewise do nothing.
    tree = expander.visit_once(tree)  # -> Done(body=...)
    tree = expander.visit_once(unastify(tree.body))  # On wrong kind of input, `unastify` will `TypeError` for us.
    return q(tree, syntax=syntax, expander=expander, **kw)

def expand(tree, *, syntax, expander, **kw):
    '''[syntax, expr/block] expand quasiquoted `tree` until no macros remain.

    The result remains in quasiquoted form.

    Like calling `expander.visit_recursively(tree)`, but for quasiquoted `tree`.

    `tree` must be a quasiquoted AST; i.e. output from, or an invocation of,
    `q`, `expand1q`, `expandq`, `expand1`, or `expand`. Passing any other AST
    as `tree` raises `TypeError`.

    If your `tree` is not quasiquoted, `expandq[...]` is a shorthand for
    `expand[q[...]]`.
    '''
    if syntax not in ("expr", "block"):
        raise SyntaxError("expand is an expr and block macro only")
    tree = expander.visit_once(tree)  # make the quotes inside this invocation expand first; -> Done(body=...)
    # Always use recursive mode, because `expand[...]` may appear inside
    # another macro invocation that uses `visit_once` (which sets the expander
    # mode to non-recursive for the dynamic extent of the visit).
    tree = expander.visit_recursively(unastify(tree.body))
    return q(tree, syntax=syntax, expander=expander, **kw)
