import os
import dask.threaded
import dis
import dill
import joblib
import marshal

from collections import namedtuple
from dask.compatibility import apply
from .utils import hashabledict


Call = namedtuple('Call', ['func', 'args', 'kwargs'])

class DataCache:

    def __init__(self, directory='./dc'):
        if not os.path.exists(directory):
            os.mkdir(directory)

        self.directory = directory
        self.graph = {}
        self.step_graph = {}
        self.observed = []

    def step(self, target, *args, **kwargs):
        name = target.__name__
        trail = Call(name, args, hashabledict(kwargs))
        return Step(self, trail, target, args, kwargs)

    def record(self, target, *args, **kwargs):
        processed_args = []
        for arg in args:
            if isinstance(arg, Step):
                arg = arg.get()

            processed_args.append(arg)
        result = target(*processed_args, **kwargs)

        self.observed.append((target.__name__, args, kwargs, result))
        return result

    def summary(self):
        # We need to infer the pipeline from our graph.
        lines = []
        for name, args, kwargs, result in self.observed:
            args = tuple(a if not isinstance(a, Step)
                         else a.name for a in args)

            lines.append(name + str(args) + ' = ' + str(result))

        return '\n'.join(lines)

    def store(self, name, value):
        return joblib.dump(value, os.path.join(self.directory, make_path(name)))

    def load(self, name):
        return joblib.load(os.path.join(self.directory, make_path(name)))

    def load_hash(self, name):
        hash_file = os.path.join(self.directory, make_path(name) + '.hash')
        if not os.path.exists(hash_file):
            return None
        else:
            return open(hash_file).read()

    def store_hash(self, name, hash):
        hash_file = os.path.join(self.directory, make_path(name) + '.hash')
        with open(hash_file, 'w') as fd:
            fd.write(hash)

def make_path(name_tuple):
    return joblib.hash(name_tuple)

class Step:

    def __init__(self, dc, trail, target, args, kwargs):
        self.dc = dc
        self.target = target
        self.trail = trail
        self.args = args
        self.kwargs = kwargs

        self.dc.step_graph[self.trail] = self
        self.recompute()

    def recompute(self):
        self.dc.graph[self.trail] = (apply_with_kwargs, self.target,
                                    list(self.args), list(self.kwargs))

    def step(self, target, *args, **kwargs):
        name = target.__name__

        # We prepend our result as our first argument to allow easy chaining
        args = (self.trail,) + args

        # Our trail gets expanded
        trail = Call(name, args, hashabledict(kwargs))

        return Step(self.dc, trail, target, args, kwargs)

    def get(self):
        result = dask.threaded.get(self.dc.graph, self.trail)
        return result

    def checkpoint(self, recompute=False):
        hash_ = self.hash()

        if hash_ != self.dc.load_hash(self.trail) or recompute:
            # If hash has changed, we write stuff to disk
            self.recompute()
            result = self.get()
            self.dc.store(self.trail + ('.store',), result)
            self.dc.store_hash(self.trail, hash_)

        # We replace the calculation with the cached value
        self.dc.graph[self.trail] = (self.dc.load, self.trail + ('.store',))
        return self

    def previous(self):
        for a in self.trail.args:
            if isinstance(a, Call):
                yield self.dc.step_graph[a]

        for k, v in self.trail.kwargs:
            if isinstance(v, Call):
                yield self.dc.step_graph[v]

        return None

    def has_deps(self):
        return (any(isinstance(a, Call) for a in self.trail.args)
                or any(isinstance(v, Call) for v in self.trail.kwargs.values()))

    def hash(self):
        uniquity = (self.trail, self.args, self.kwargs,
                    self.target.__code__.co_code,
                    self.target.__code__.co_consts)

        if self.has_deps():
            previous_hash = ''.join(p.hash() for p in self.previous())
        else:
            previous_hash = ''

        return previous_hash + joblib.hash(uniquity)

    def record(self):
        return self.dc.record(self.target, *self.args, **self.kwargs)

    def prepr(self):
        '''Pretty representation'''
        path = []


        func_name = self.trail.func

        args = ', '.join('*' if isinstance(a, Call) else str(a) for a in self.trail.args)
        kwargs = ','.join(k + '=' + str(v) for k, v in self.trail.kwargs.items())

        if args == '' and kwargs == '':
            tpl = "{}{}{}"

        elif args == '' and kwargs != '' or kwargs == '' and args != '':
            tpl = "{}({}{})"

        else:
            tpl = "{}({}, {})"

        path.append(tpl.format(self.trail.func, args, kwargs))
        for p in self.previous():
            path.append(p.prepr())

        return '<-'.join(path)

def apply_with_kwargs(function, args, kwargs):
    return function(*args, **dict(kwargs))