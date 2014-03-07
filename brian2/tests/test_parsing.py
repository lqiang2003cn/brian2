'''
Tests the brian2.parsing package
'''
from collections import namedtuple

from numpy.testing import assert_allclose, assert_raises
import numpy as np

from brian2.core.preferences import brian_prefs
from brian2.core.variables import Constant
from brian2.core.functions import Function
from brian2.groups.group import Group
from brian2.utils.stringtools import get_identifiers, deindent
from brian2.parsing.rendering import (NodeRenderer, NumpyNodeRenderer,
                                      CPPNodeRenderer,
                                      )
from brian2.parsing.dependencies import abstract_code_dependencies
from brian2.parsing.expressions import (is_boolean_expression,
                                        parse_expression_unit,
                                        _get_value_from_expression)
from brian2.parsing.sympytools import str_to_sympy, sympy_to_str
from brian2.parsing.functions import (abstract_code_from_function,
                                      extract_abstract_code_functions,
                                      substitute_abstract_code_functions)
from brian2.units import (volt, amp, DimensionMismatchError,
                          have_same_dimensions, Unit, get_unit)

try:
    from scipy import weave
except ImportError:
    weave = None
import nose


# a simple Group for testing
class SimpleGroup(Group):
    def __init__(self, variables, namespace=None):
        self.variables = variables
        self.namespace = namespace

# FIXME: This shouldn't be only used for testing
def namespace_to_variable(name, group):
    value = group.resolve(name)
    if isinstance(value, Function):
        return value
    else:
        unit = get_unit(value)
        array_value = np.asarray(value)
        return Constant(name, unit=unit, value=array_value)

# TODO: add some tests with e.g. 1.0%2.0 etc. once this is implemented in C++
TEST_EXPRESSIONS = '''
    a+b+c*d-f+g-(b+d)-(a-c)
    a**b**2
    a**(b**2)
    (a**b)**2
    a*(b+c*(a+b)*(a-(c*d)))
    a/b/c-a/(b/c)
    a<b
    a<=b
    a>b
    a>=b
    a==b
    a!=b
    a+1
    1+a
    1+3
    a>0.5 and b>0.5
    a>0.5 and b>0.5 or c>0.5
    a>0.5 and b>0.5 or not c>0.5
    2%4
    '''


def parse_expressions(renderer, evaluator, numvalues=10):
    exprs = [([m for m in get_identifiers(l) if len(m)==1], [], l.strip())
             for l in TEST_EXPRESSIONS.split('\n') if l.strip()]
    i, imod = 1, 33
    for varids, funcids, expr in exprs:
        pexpr = renderer.render_expr(expr)
        n = 0
        for _ in xrange(numvalues):
            # assign some random values
            ns = {}
            for v in varids:
                ns[v] = float(i)/imod
                i = i%imod+1
            r1 = eval(expr.replace('&', ' and ').replace('|', ' or '), ns)
            n += 1
            r2 = evaluator(pexpr, ns)
            try:
                # Use all close because we can introduce small numerical
                # difference through sympy's rearrangements
                assert_allclose(r1, r2)
            except AssertionError as e:
                raise AssertionError("In expression " + str(expr) +
                                     " translated to " + str(pexpr) +
                                     " " + str(e))


def numpy_evaluator(expr, userns):
    ns = {}
    #exec 'from numpy import logical_not' in ns
    ns['logical_not'] = np.logical_not
    ns.update(**userns)
    for k in userns.keys():
        if not k.startswith('_'):
            ns[k] = np.array([userns[k]])
    try:
        x = eval(expr, ns)
    except Exception as e:
        raise ValueError("Could not evaluate numpy expression "+expr+" exception "+str(e))
    if isinstance(x, np.ndarray):
        return x[0]
    else:
        return x
    
    
def cpp_evaluator(expr, ns):
    if weave is not None:
        return weave.inline('return_val = %s;' % expr, ns.keys(), local_dict=ns,
                            compiler=brian_prefs['codegen.runtime.weave.compiler'],
                            extra_compile_args=brian_prefs['codegen.runtime.weave.extra_compile_args'],
                            include_dirs=brian_prefs['codegen.runtime.weave.include_dirs']
                            )
    else:
        raise nose.SkipTest('No weave support.')


def test_parse_expressions_python():
    parse_expressions(NodeRenderer(), eval)


def test_parse_expressions_numpy():
    parse_expressions(NumpyNodeRenderer(), numpy_evaluator)


def test_parse_expressions_cpp():
    parse_expressions(CPPNodeRenderer(), cpp_evaluator)


def test_parse_expressions_sympy():
    # sympy is about symbolic calculation, the string returned by the renderer
    # contains "Symbol('a')" etc. so we cannot simply evaluate it in a
    # namespace.
    # We therefore use a different approach: Convert the expression to a
    # sympy expression via str_to_sympy (uses the SympyNodeRenderer internally),
    # then convert it back to a string via sympy_to_str and evaluate it

    class SympyRenderer(object):
        def render_expr(self, expr):
            return str_to_sympy(expr)

    def evaluator(expr, ns):
        expr = sympy_to_str(expr)
        return eval(expr, ns)

    parse_expressions(SympyRenderer(), evaluator)


def test_abstract_code_dependencies():
    code = '''
    a = b+c
    d = b+c
    a = func_a()
    a = func_b()
    a = x+d
    '''
    known_vars = set(['a', 'b', 'c'])
    known_funcs = set(['func_a'])
    res = abstract_code_dependencies(code, known_vars, known_funcs)
    expected_res = dict(
        all=['a', 'b', 'c', 'd', 'x',
             'func_a', 'func_b',
             ],
        read=['b', 'c', 'd', 'x'],
        write=['a', 'd'],
        funcs=['func_a', 'func_b'],
        known_all=['a', 'b', 'c', 'func_a'],
        known_read=['b', 'c'],
        known_write=['a'],
        known_funcs=['func_a'],
        unknown_read=['d', 'x'],
        unknown_write=['d'],
        unknown_funcs=['func_b'],
        undefined_read=['x'],
        newly_defined=['d'],
        )
    for k, v in expected_res.items():
        if not getattr(res, k)==set(v):
            raise AssertionError("For '%s' result is %s expected %s" % (
                                        k, getattr(res, k), set(v)))


def test_is_boolean_expression():
    # dummy "Variable" class
    Var = namedtuple("Var", ['is_bool'])

    # dummy function object
    class Func(object):
        def __init__(self, returns_bool=False):
            self._returns_bool = returns_bool

    # variables / functions
    a = Constant('a', unit=Unit(1), value=True)
    b = Constant('b', unit=Unit(1), value=False)
    c = Constant('c', unit=Unit(1), value=5)
    f = Func(returns_bool=True)
    g = Func(returns_bool=False)
    s1 = Var(is_bool=True)
    s2 = Var(is_bool=False)

    variables = {'a': a, 'b': b, 'c': c, 'f': f, 'g': g, 's1': s1, 's2': s2}

    EVF = [
        (True, 'a or b'),
        (False, 'c'),
        (False, 's2'),
        (False, 'g(s1)'),
        (True, 's2 > c'),
        (True, 'c > 5'),
        (True, 'True'),
        (True, 'a<b'),
        (True, 'not (a>=b)'),
        (False, 'a+b'),
        (True, 'f(c)'),
        (False, 'g(c)'),
        (True, 'f(c) or a<b and s1', ),
        ]
    for expect, expr in EVF:
        ret_val = is_boolean_expression(expr, variables)
        if expect != ret_val:
            raise AssertionError(('is_boolean_expression(%r) returned %s, '
                                  'but was supposed to return %s') % (expr,
                                                                      ret_val,
                                                                      expect))
    assert_raises(SyntaxError, is_boolean_expression, 'a<b and c',
                  variables)
    assert_raises(SyntaxError, is_boolean_expression, 'a or foo',
                  variables)
    assert_raises(SyntaxError, is_boolean_expression, 'ot a', # typo
                  variables)
    assert_raises(SyntaxError, is_boolean_expression, 'g(c) and f(a)',
                  variables)
    
    
def test_parse_expression_unit():
    Var = namedtuple('Var', ['unit'])
    variables = {'a': Var(unit=volt*amp),
                 'b': Var(unit=volt),
                 'c': Var(unit=amp)}
    group = SimpleGroup(namespace={}, variables=variables)
    EE = [
        (volt*amp, 'a+b*c'),
        (DimensionMismatchError, 'a+b'),
        (DimensionMismatchError, 'a<b'),
        (1, 'a<b*c'),
        (1, 'a or b'),
        (1, 'not (a >= b*c)'),
        (DimensionMismatchError, 'a or b<c'),
        (1, 'a/(b*c)<1'),
        (1, 'a/(a-a)'),
        (1, 'a<mV*mA'),
        (volt**2, 'b**2'),
        (volt*amp, 'a%(b*c)'),
        (volt, '-b'),
        (1, '(a/a)**(a/a)'),
        # Expressions involving functions
        (volt, 'rand()*b'),
        (volt**0.5, 'sqrt(b)'),
        (volt, 'ceil(b)'),
        (volt, 'sqrt(randn()*b**2)'),
        (1, 'sin(b/b)'),
        (DimensionMismatchError, 'sin(b)'),
        (DimensionMismatchError, 'sqrt(b) + b')
        ]
    for expect, expr in EE:
        all_variables = {}
        for name in get_identifiers(expr):
            if name in variables:
                all_variables[name] = variables[name]
            else:
                all_variables[name] = namespace_to_variable(name, group)

        if expect is DimensionMismatchError:
            assert_raises(DimensionMismatchError, parse_expression_unit, expr,
                          all_variables)
        else:
            u = parse_expression_unit(expr, all_variables)
            assert have_same_dimensions(u, expect)

    wrong_expressions = ['a**b',
                         'a << b',
                         'int(True' # typo
                        ]
    for expr in wrong_expressions:
        all_variables = {}
        for name in get_identifiers(expr):
            if name in variables:
                all_variables[name] = variables[name]
            else:
                all_variables[name] = namespace_to_variable(name, group)
        assert_raises(SyntaxError, parse_expression_unit, expr, all_variables)


def test_value_from_expression():
    # This function is used to get the value of an exponent, necessary for unit checking

    constants = {'c': 3}
    # dummy class
    class C(object):
        pass
    variables = {'s_constant_scalar': C(), 's_non_constant': C(),
                  's_non_scalar': C()}
    variables['s_constant_scalar'].scalar = True
    variables['s_constant_scalar'].constant = True
    variables['s_constant_scalar'].get_value = lambda: 2.0
    variables['s_non_scalar'].constant = True
    variables['s_non_constant'].scalar = True
    variables['c'] = Constant('c', unit=Unit(1), value=3)

    expressions = ['1', '-0.5', 'c', '2**c', '(c + 3) * 5',
                   'c + s_constant_scalar', 'True', 'False']

    for expr in expressions:
        eval_expr = expr.replace('s_constant_scalar', 's_constant_scalar.get_value()')
        assert float(eval(eval_expr, variables, constants)) == _get_value_from_expression(expr,
                                                                                          variables)

    wrong_expressions = ['s_non_constant', 's_non_scalar', 'c or True']
    for expr in wrong_expressions:
        assert_raises(SyntaxError, lambda : _get_value_from_expression(expr,
                                                                       variables))


def test_abstract_code_from_function():
    # test basic functioning
    def f(x):
        y = x+1
        return y*y
    ac = abstract_code_from_function(f)
    assert ac.name=='f'
    assert ac.args==['x']
    assert ac.code.strip()=='y = x + 1'
    assert ac.return_expr=='y * y'
    # Check that unsupported features raise an error

    def f(x):
        return x[:]
    assert_raises(SyntaxError, abstract_code_from_function, f)

    def f(x, **kwarg):
        return x
    assert_raises(SyntaxError, abstract_code_from_function, f)

    def f(x, *args):
        return x
    assert_raises(SyntaxError, abstract_code_from_function, f)



def test_extract_abstract_code_functions():
    code = '''
    def f(x):
        return x*x
        
    def g(V):
        V += 1
        
    irrelevant_code_here()
    '''
    funcs = extract_abstract_code_functions(code)
    assert funcs['f'].return_expr == 'x * x'
    assert funcs['g'].args == ['V']


def test_substitute_abstract_code_functions():
    def f(x):
        y = x*x
        return y
    def g(x):
        return f(x)+1
    code = '''
    z = f(x)
    z = f(x)+f(y)
    w = f(z)
    h = f(f(w))
    p = g(g(x))
    '''
    funcs = [abstract_code_from_function(f),
             abstract_code_from_function(g),
             ]
    subcode = substitute_abstract_code_functions(code, funcs)
    for x, y in [(0, 1), (1, 0), (0.124323, 0.4549483)]:
        ns1 = {'x':x, 'y':y, 'f':f, 'g':g}
        ns2 = {'x':x, 'y':y}
        exec deindent(code) in ns1
        exec subcode in ns2
        for k in ['z', 'w', 'h', 'p']:
            assert ns1[k]==ns2[k]
    
    
if __name__=='__main__':
    test_parse_expressions_python()
    test_parse_expressions_numpy()
    #test_parse_expressions_cpp()
    test_parse_expressions_sympy()
    test_abstract_code_dependencies()
    test_is_boolean_expression()
    test_parse_expression_unit()
    test_value_from_expression()
    test_abstract_code_from_function()
    test_extract_abstract_code_functions()
    test_substitute_abstract_code_functions()
    