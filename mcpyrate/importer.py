# -*- coding: utf-8; -*-
"""Importer (finder/loader) customizations, to inject the macro expander."""

__all__ = ["source_to_xcode", "path_xstats", "invalidate_xcaches"]

import ast
from copy import copy, deepcopy
import distutils.sysconfig
import importlib.util
from importlib.machinery import FileFinder, SourceFileLoader
import tokenize
import os
import pickle
import sys
from types import ModuleType

from .dialects import expand_dialects
from .expander import find_macros, expand_macros, destructure_candidate, global_postprocess
from .coreutils import resolve_package, ismacroimport
from .markers import check_no_markers_remaining
from .unparser import unparse_with_fallbacks
from .utils import format_location


# --------------------------------------------------------------------------------
# multi-phase compilation support (so a module can define macros it uses itself)

# TODO: add a `phase` "macro" to `mcpyrate.core` (it should always error out, this is an importer feature)

def iswithphase(stmt):
    """Detect `with phase[n]`, where `n >= 1` is an integer.

    Return `n`, or `False`.
    """
    if type(stmt) is not ast.With:
        return False
    if len(stmt.items) != 1:
        return False
    item = stmt.items[0]
    if item.optional_vars is not None:  # no as-part allowed
        return False
    candidate = item.context_expr
    if type(candidate) is not ast.Subscript:
        return False
    macroname, macroargs = destructure_candidate(candidate)
    if macroname != "phase":
        return False
    if not macroargs or len(macroargs) != 1:  # exactly one macro-argument
        return False
    arg = macroargs[0]
    if type(arg) is ast.Constant:
        n = arg.value
    elif type(arg) is ast.Num:  # TODO: remove ast.Num once we bump minimum language version to Python 3.8
        n = arg.n
    else:
        return False
    if not isinstance(n, int) or n < 1:
        return False
    return n


def detect_highest_phase(tree):
    """Scan a module body for `with phase[n]` statements and return highest `n`, or `None`.

    Primarily meant to be called with `tree` the AST of a module that
    uses macros, but works with any `tree` that has a `body` attribute.

    Used for initializing the phase countdown in multi-phase compilation.
    """
    maxn = None
    for stmt in tree.body:
        n = iswithphase(stmt)
        if maxn is None or (n is not None and n > maxn):
            maxn = n
    return maxn


def split_multiphase_module_ast(tree, *, phase=0):
    """Split `ast.Module` `tree` into given phase and remaining parts.

    The statements belonging to this phase are returned as a new `ast.Module`,
    and the `body` attribute of the original `tree` is overwritten with the
    remaining statements.
    """
    if not isinstance(phase, int):
        raise TypeError  # TODO: proper error message
    if phase < 0:
        raise ValueError  # TODO: proper error message
    if phase == 0:
        return tree
    remaining = []
    thisphase = []
    for stmt in tree.body:
        if iswithphase(stmt) == phase:
            thisphase.extend(stmt.body)
        else:
            remaining.append(stmt)
    tree.body[:] = remaining
    out = copy(tree)
    out.body = thisphase
    return out


def multiphase_compile(tree, *, filename, self_module, start_from_phase=None, _optimize=-1):
    """Compile an AST in multiple phases, controlled by `with phase[n]`.

    Primarily meant to be called with `tree` the AST of a module that
    uses macros, but works with any `tree` that has a `body` attribute.

    At each phase `k >= 1`, a temporary module is injected to `sys.modules`,
    so that the next phase can import macros from it (using a self-macro-import).
    Once phase `k = 0` is reached and the code is compiled, the temporary entry
    in `sys.modules` is deleted.

    `filename`:         Full path to the `.py` file being compiled.

    `self_module`:      Absolute dotted module name of the module being compiled.
                        Will be used to temporarily inject the temporary,
                        higher-phase modules into `sys.modules`.

    `start_from_phase`: Optional int, >= 0. If not provided, will be scanned
                        automatically from `tree`, using `detect_highest_phase`.

                        This parameter exists only so that if you have already
                        scanned `tree` to determine the highest phase (e.g. in
                        order to detect whether the module needs multi-phase
                        compilation), you can provide the value, so this
                        function doesn't need to scan `tree` again.

    `_optimize`:        Passed on to Python's built-in `compile` function, when compiling
                        the temporary higher-phase modules.

    Return value is the final phase-0 `tree`, after macro expansion.
    """
    n = start_from_phase or detect_highest_phase(tree)

    # TODO: preserve ordering of code in the file. Currently we just paste in phase-descending order.
    for k in range(n, -1, -1):  # phase 0 is what a regular compile would do
        print(f"***** PHASE {k} for '{self_module}' ({filename})")  # TODO: add proper debug tools
        phase_k_tree = split_multiphase_module_ast(tree, phase=k)
        if phase_k_tree:
            # Establish macro bindings, but don't transform macro-imports yet; we need to
            # keep them in the code to be spliced into the next phase.
            module_macro_bindings = find_macros(phase_k_tree, filename=filename,
                                                self_module=self_module, transform=False)

            # Expand macros as usual.
            expansion = expand_macros(phase_k_tree, bindings=module_macro_bindings, filename=filename)

            # # TODO: add proper debug tools
            # from .unparser import unparse
            # print(unparse(expansion.body, debug=True, color=True))

            # Lift macro-expanded code from phase `k` into phase `k - 1`.
            if k > 0:
                tree.body[:] = deepcopy(expansion.body) + tree.body

            # Transform the macro-imports in the code we actually intend to run at phase k.
            find_macros(expansion, filename=filename, self_module=self_module, transform=True)

            # We must postprocess again, because we transformed the macro-imports *after*
            # `expand_macros` (which postprocesses), and self-macro-imports insert dummy
            # coverage nodes (which have a `Done` marker around them).
            expansion = global_postprocess(expansion)

            check_no_markers_remaining(expansion, filename=filename)

            # Once we hit the final phase, no more temporary modules - let the import system take over.
            if k == 0:
                break

            # Compile temporary module, and inject it into `sys.modules`,
            # so we can compile the next phase.
            #
            # We don't bother with hifi stuff, such as attributes usually set by the importer,
            # or even the module docstring. We essentially just need the functions for the macro bindings.
            temporary_code = compile(expansion, filename, "exec", dont_inherit=True, optimize=_optimize)
            temporary_module = ModuleType(self_module)
            sys.modules[self_module] = temporary_module
            exec(temporary_code, temporary_module.__dict__)

    if self_module in sys.modules:  # delete temporary module
        del sys.modules[self_module]

    return expansion

# --------------------------------------------------------------------------------

def source_to_xcode(self, data, path, *, _optimize=-1):
    """[mcpyrate] Expand dialects, then expand macros, then compile.

    Intercepts the source to bytecode transformation.
    """
    tree = expand_dialects(data, filename=path)

    n = detect_highest_phase(tree)
    if not n:  # no `with phase[n]`; regular one-phase compilation
        module_macro_bindings = find_macros(tree, filename=path)
        expansion = expand_macros(tree, bindings=module_macro_bindings, filename=path)
        check_no_markers_remaining(expansion, filename=path)

    else:
        # `self.name` is absolute dotted module name, see `importlib.machinery.FileLoader`.
        # This allows us to support `from __self__ import macros, ...` for multi-phase
        # compilation (a.k.a. `with phase`).
        expansion = multiphase_compile(tree, filename=path, self_module=self.name,
                                       start_from_phase=n, _optimize=_optimize)

    return compile(expansion, path, "exec", dont_inherit=True, optimize=_optimize)


# TODO: Support PEP552 (Deterministic pycs). Need to intercept source file hashing, too.
# TODO: https://www.python.org/dev/peps/pep-0552/
_stdlib_path_stats = SourceFileLoader.path_stats
_xstats_cache = {}
def path_xstats(self, path):
    """[mcpyrate] Compute a `.py` source file's mtime, accounting for macro-imports.

    Beside the source file `path` itself, we look at any macro definition files
    the source file imports macros from, recursively, in a `make`-like fashion.

    The mtime is the latest of those of `path` and its macro-dependencies,
    considered recursively, so that if any macro definition anywhere in the
    macro-dependency tree of `path` is changed, Python will treat the source
    file `path` as "changed", thus re-expanding and recompiling `path` (hence,
    updating the corresponding `.pyc`).

    If `path` does not end in `.py`, delegate to the standard implementation
    of `SourceFileLoader.path_stats`.
    """
    # Ignore stdlib, it's big and doesn't use macros. Allows faster error
    # exits, because an uncaught exception causes Python to load a ton of
    # .py based stdlib modules. Also makes `macropython -i` start faster.
    if path in _stdlib_sourcefile_paths or not path.endswith(".py"):
        return _stdlib_path_stats(self, path)
    if path in _xstats_cache:
        return _xstats_cache[path]

    stat_result = os.stat(path)

    # Try for cached macro-import statements for `path` to avoid the parse cost.
    #
    # This is a single node in the dependency graph; the result depends only
    # on the content of the source file `path` itself. So we invalidate the
    # macro-import statement cache for `path` based on the mtime of `path` only.
    #
    # For a given source file `path`, the `.pyc` sometimes becomes newer than
    # the macro-dependency cache. This is normal. Unlike the bytecode, the
    # macro-dependency cache only needs to be refreshed when the text of the
    # source file `path` changes.
    #
    # So if some of the macro-dependency source files have changed (so `path`
    # must be re-expanded and recompiled), but `path` itself hasn't, the text
    # of the source file `path` will still have the same macro-imports it did
    # last time.
    #
    pycpath = importlib.util.cache_from_source(path)
    if pycpath.endswith(".pyc"):
        pycpath = pycpath[:-4]
    importcachepath = pycpath + ".mcpyrate.pickle"
    try:
        cache_valid = False
        with open(importcachepath, "rb") as importcachefile:
            data = pickle.load(importcachefile)
        if data["st_mtime_ns"] == stat_result.st_mtime_ns:
            cache_valid = True
    except Exception:
        pass

    if cache_valid:
        macro_and_dialect_imports = data["macroimports"] + data["dialectimports"]
        has_relative_macroimports = data["has_relative_macroimports"]
    else:
        # This can be slow, the point of `.pyc` is to avoid the parse-and-compile cost.
        # We do save the macro-expansion cost, though, and that's likely much more expensive.
        #
        # TODO: Dialects may inject imports in the template that the dialect transformer itself
        # TODO: doesn't need. How to detect those? Regex-search the source text?
        with tokenize.open(path) as sourcefile:
            tree = ast.parse(sourcefile.read())
        macroimports = [stmt for stmt in tree.body if ismacroimport(stmt)]
        dialectimports = [stmt for stmt in tree.body if ismacroimport(stmt, magicname="dialects")]

        macro_and_dialect_imports = macroimports + dialectimports
        has_relative_macroimports = any(macroimport.level for macroimport in macro_and_dialect_imports)

        # macro-import statement cache goes with the .pyc
        if not sys.dont_write_bytecode:
            data = {"st_mtime_ns": stat_result.st_mtime_ns,
                    "macroimports": macroimports,
                    "dialectimports": dialectimports,
                    "has_relative_macroimports": has_relative_macroimports}
            try:
                with open(importcachepath, "wb") as importcachefile:
                    pickle.dump(data, importcachefile)
            except Exception:
                pass

    # The rest of the lookup process depends on the configuration of the currently
    # running Python, particularly its `sys.path`, so we do it dynamically.
    #
    # TODO: some duplication with code in mcpyrate.coreutils.get_macros, including the error messages.
    package_absname = None
    if has_relative_macroimports:
        try:
            package_absname = resolve_package(path)
        except (ValueError, ImportError) as err:
            raise ImportError(f"while resolving absolute package name of {path}, which uses relative macro-imports") from err

    mtimes = []
    for macroimport in macro_and_dialect_imports:
        if macroimport.module is None:
            approx_sourcecode = unparse_with_fallbacks(macroimport)
            loc = format_location(path, macroimport, approx_sourcecode)
            raise SyntaxError(f"{loc}\nmissing module name in macro-import")
        module_absname = importlib.util.resolve_name('.' * macroimport.level + macroimport.module, package_absname)

        spec = importlib.util.find_spec(module_absname)
        if spec:  # self-macro-imports have no `spec`, and that's fine.
            origin = spec.origin
            stats = path_xstats(self, origin)
            mtimes.append(stats["mtime"])

    mtime = stat_result.st_mtime_ns * 1e-9
    # size = stat_result.st_size
    mtimes.append(mtime)

    result = {"mtime": max(mtimes)}  # and sum(sizes)? OTOH, as of Python 3.8, only 'mtime' is mandatory.
    if sys.version_info >= (3, 7, 0):
        # Docs say `size` is optional, and this is correct in 3.6 (and in PyPy3 7.3.0):
        # https://docs.python.org/3/library/importlib.html#importlib.abc.SourceLoader.path_stats
        #
        # but in 3.7 and later, the implementation is expecting at least a `None` there,
        # if the `size` is not used. See `get_code` in:
        # https://github.com/python/cpython/blob/master/Lib/importlib/_bootstrap_external.py
        result["size"] = None
    _xstats_cache[path] = result
    return result


_stdlib_invalidate_caches = FileFinder.invalidate_caches
def invalidate_xcaches(self):
    '''[mcpyrate] Clear the macro dependency tree cache.

    Then delegate to the standard implementation of `FileFinder.invalidate_caches`.
    '''
    _xstats_cache.clear()
    return _stdlib_invalidate_caches(self)


def _detect_stdlib_sourcefile_paths():
    '''Return a set of full paths of `.py` files that are part of Python's standard library.'''
    # Adapted from StackOverflow answer by Adam Spiers, https://stackoverflow.com/a/8992937
    # Note we don't want to get module names, but full paths to `.py` files.
    stdlib_dir = distutils.sysconfig.get_python_lib(standard_lib=True)
    paths = set()
    for root, dirs, files in os.walk(stdlib_dir):
        for filename in files:
            if filename[-3:] == ".py":
                paths.add(os.path.join(root, filename))
    return paths
_stdlib_sourcefile_paths = _detect_stdlib_sourcefile_paths()
