"""Small strict input primitives. No third-party runtime dependencies."""
import re
from fractions import Fraction

def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError(f'expected fields {required}; optional {optional}')
    return value

def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'integer in [{low},{high}] required')
    return value

def array(value, low, high):
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise ValueError(f'array length in [{low},{high}] required')
    return value

def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,64}', value):
        raise ValueError('invalid identifier')
    return value

def unique(values):
    if len(set(values)) != len(values):
        raise ValueError('duplicate identity')

def decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,8})(?:[.][0-9]{1,6})?', value):
        raise ValueError('bounded decimal string required')
    return Fraction(value)

def exact(value):
    return str(value)
