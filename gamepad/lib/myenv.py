# Copyright 2018 The GamePad Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from coq.constr import Name
from lib.myutil import NotFound


class MyEnv(object):
    """
    Environment for interpreter-inspired embeddings.
    """
    def __init__(self, env, order):
        self.env = env
        self.order = order

    def extend(self, ident, value):
        assert isinstance(ident, Name)
        env_p = {}
        for k, v in self.env.items():
            env_p[k] = v
        env_p[ident] = value
        return MyEnv(env_p, self.order + [value])

    def lookup_id(self, ident):
        if ident in self.env:
            return self.env[ident]
        else:
            raise NotFound("Lookup failure of {} in env [{}]".format(
                           ident, self.dump()))

    def lookup_rel(self, idx):
        if idx < len(self.order):
            return self.order[-1-idx]
        else:
            raise NotFound("Lookup failure of {} in env [{}]".format(
                           idx, self.dump()))

    def dump(self):
        # TODO(deh): something wrong with dumping code when printing v
        xs = (["{}".format(k) for k, v in self.env.items()])
        return ", ".join(xs)


class FastEnv(object):
    """
    Environment for interpreter-inspired embeddings. Distinguishes
    local environment (lambdas) from context environment (proof context).
    """
    def __init__(self, ctx_env, local_env, ctx_order, local_order):
        self.ctx_env = ctx_env
        self.local_env = local_env
        self.ctx_order = ctx_order
        self.local_order = local_order

    def ctx_extend(self, ident, value):
        assert isinstance(ident, Name)
        self.ctx_env[ident] = value
        self.ctx_order.append(value)
        return FastEnv(self.ctx_env, self.local_env, self.ctx_order, self.local_order)

    def local_extend(self, ident, value):
        assert isinstance(ident, Name)
        local_env_p = {}
        for k, v in self.local_env.items():
            local_env_p[k] = v
        local_env_p[ident] = value
        return FastEnv(self.ctx_env, local_env_p, self.ctx_order, self.local_order + [value])

    def lookup_id(self, ident):
        if ident in self.local_env:
            return self.local_env[ident]
        elif ident in self.ctx_env:
            return self.ctx_env[ident]
        else:
            raise NotFound("Lookup failure of {} in env [{}]".format(
                           ident, self.dump()))

    def lookup_rel(self, idx):
        if idx < len(self.local_order):
            return self.local_order[-1-idx]
        else:
            raise NotFound("Lookup failure of {} in env [{}]".format(
                           idx, self.dump()))

    def dump(self):
        # TODO(deh): something wrong with dumping code when printing v
        xs = ["ctx:"] + ["{}".format(k) for k, v in self.ctx_env.items()] + ["\nlocal:"] + ["{}".format(k) for k, v in self.local_env.items()]

        return ", ".join(xs)