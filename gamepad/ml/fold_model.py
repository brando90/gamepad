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

import numpy as np

import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional

from coq.constr import *
from coq.glob_constr import *
from lib.myenv import FastEnv
from lib.myutil import NotFound
import ml.torchfold as ptf


"""
[Note]

Version that uses torchfold
1. Embed Coq tactic trees into R^D vectors
2. Model uses embeddings to obtain prediction of:
    close, medium, far
"""


# -------------------------------------------------
# Helper

def get_other(l, index):
    # return (item at index, all items in list other than at index). Handles negative indexes too
    index = index % len(l)
    return l[index], l[:index] + l[index+1:]


def seq_embed(name, folder, xs, init, get_hiddens, ln, input_dropout, conclu_pos):
    # Preprocess for fold
    hidden = folder.add('identity', init)

    # Input dropout
    if input_dropout:
        hidden = folder.add('input_dropout_f', hidden)
        for i, x in enumerate(xs):
            xs[i] = folder.add('input_dropout_f', x)

    hiddens = []
    # Cell sequence
    for i, x in enumerate(xs):
        hidden = folder.add(name + '_cell_f', x, hidden)
        hiddens.append(hidden)

    # Weird layer-norm
    if ln:
        hidden = folder.add(name[:3] + '_ln_f', hidden)
    if get_hiddens:
        return hidden, hiddens
    else:
        return hidden


def seq_sigmoid_attn_embed(folder, xs, sv_init, ln, input_dropout, conclu_pos):
    if input_dropout:
        for i, x in enumerate(xs):
            xs[i] = folder.add('input_dropout_f', x)

    # Attention
    conclu, other = get_other(xs, conclu_pos)
    q = folder.add('attn_q_f', conclu)
    sv = folder.add('attn_identity', sv_init)
    for x in other:
        sv = folder.add('attn_sv_f', q, x, sv)

    return sv


# -------------------------------------------------
# Fold over anything

class Folder(object):
    def __init__(self, model, f_fold, f_sharing, cuda):
        # Folding state
        self.model = model
        self.f_fold = f_fold       # whether to fold or not
        self.f_sharing = f_sharing # whether to use shared embeddings
        self._folder = None        # which folder to use
        self.cuda = cuda
        self.max_batch_ops = {}
        if not self.cuda:
            # Optimisation for CPU. Ad-hoc, so might not be optimal

            # Embed lookups
            self.max_batch_ops['embed_lookup_f'] = 128

            # Cell calls
            for name in ["", "lstm", "tree"]:
                self.max_batch_ops['ast_' + name + '_cell_f'] = 32
                self.max_batch_ops['ctx_' + name + '_cell_f'] = 32

            # FC calls
            self.max_batch_ops['proj_f'] = 32
            self.max_batch_ops['final_f'] = 32
        self.reset()

    def reset(self):
        """Reset folding state"""
        if self.f_fold:
            self._folder = ptf.Fold(max_batch_ops=self.max_batch_ops)
        else:
            self._folder = ptf.Unfold(self.model)
        if self.cuda:
            self._folder.cuda()

    def apply(self, *args):
        """Call after folding entire tactic state to force computation"""
        return self._folder.apply(self.model, args)

    def add(self, op, *args):
        return self._folder.add(op, *args)

    def __str__(self):
        return str(self._folder)


# -------------------------------------------------
# Fold over tactic state

class TacStFolder(object):
    def __init__(self, model, tactr, folder):
        self.model = model    # Only used to access embeddings
        self.tactr = tactr    # Corresponding tactic tree

        self.folder = folder
        self.folded = {}
        self.f_sharing = folder.f_sharing
        if folder.cuda:
            self.torch = torch.cuda
        else:
            self.torch = torch

        if self.model.f_mid:
            self.get_tacst = lambda pt: pt.mid_tacst()
            self.fold_ast = lambda env, gc: self._fold_mid(env, gc)
            self.decode = lambda idx: tactr.mid_decoder.decode_exp_by_key(idx)
        else:
            self.get_tacst = lambda pt: pt.kern_tacst()
            self.fold_ast = lambda env, c: self._fold_ast(env, Kind.TYPE, c)
            self.decode = lambda idx: tactr.decoder.decode_exp_by_key(idx)

    def reset(self):
        self.folded = {}

    # -------------------------------------------
    # Tactic state folding (Kernel)

    def fold_tacst(self, tacst_pt):
        """Top-level fold function"""
        tacst = self.get_tacst(tacst_pt)
        gid, ctx, concl_idx, tac = tacst
        env, foldeds = self.fold_ctx(gid, ctx)
        folded = self.fold_concl(gid, env, concl_idx)
        return self.model.pred(self.folder, folded, *foldeds)

    def fold_ctx(self, gid, ctx):
        foldeds = []
        env = FastEnv({}, {}, [], [])
        for ident, typ_idx in ctx:
            folded = self.fold_ctx_ident(gid, env, typ_idx)
            env = env.ctx_extend(Name(ident), folded)
            foldeds += [folded]
        return env, foldeds

    def fold_ctx_ident(self, gid, env, typ_idx):
        # NOTE(deh): Do not need context sharing because of AST sharing
        c = self.decode(typ_idx)
        return self.fold_ast(env, c)

    def fold_concl(self, gid, env, concl_idx):
        # NOTE(deh): Do not need conclusion sharing because of AST sharing
        c = self.decode(concl_idx)
        return self.fold_ast(env, c)

    # -------------------------------------------
    # Kernel-level AST folding

    def _fold(self, key, args):
        fold = self.model.ast_emb_func(self.folder, args)
        self.folded[key] = fold
        return fold

    def _fold_ast(self, env, kind, c):
        key = c.tag
        if self.f_sharing:
            if key in self.folded:
                return self.folded[key]

        # Ordered by number of occurances, better would be a dict.
        typ = type(c)
        if typ is AppExp:
            ev_c = self._fold_ast(env, Kind.TERM, c.c)
            ev_cs = self._fold_asts(env, Kind.TERM, c.cs)
            return self._fold(key, [self.model.app, ev_c, *ev_cs])
        elif typ is ConstExp:
            ev_const = self.fold_const_name(c.const)
            # NOTE(deh): leaving out universe instances on purpose
            # ev_ui = self.fold_ui(c.ui)
            return self._fold(key, [self.model.const, ev_const])
        elif typ is VarExp:
            ev_x = env.lookup_id(Name(c.x))
            return self._fold(key, [self.model.var, ev_x])
        elif typ is ConstructExp:
            ev_ind = self.fold_ind_name(c.ind)
            ev_conid = self.fold_conid_name((c.ind, c.conid))
            # NOTE(deh): leaving out universe instances on purpose
            # ev_ui = self.fold_ui(c.ui)
            return self._fold(key, [self.model.construct, ev_ind, ev_conid])
        elif typ is IndExp:
            ev_ind = self.fold_ind_name(c.ind)
            # NOTE(deh): leaving out universe instances on purpose
            # ev_ui = self.fold_ui(c.ui)
            return self._fold(key, [self.model.ind, ev_ind])
        elif typ is RelExp:
            # NOTE(deh): DeBruinj indicides start at 1 ...
            ev_idx = env.lookup_rel(c.idx - 1)
            return self._fold(key, [self.model.rel, ev_idx])
        elif typ is ProdExp:
            ev_x = self.fold_local_var(c.ty1)
            ev_ty1 = self._fold_ast(env, Kind.TYPE, c.ty1)
            ev_ty2 = self._fold_ast(env.local_extend(c.name, ev_x), Kind.TYPE, c.ty2)
            return self._fold(key, [self.model.prod, ev_ty1, ev_ty2])
        elif typ is LambdaExp:
            ev_x = self.fold_local_var(c.ty)
            ev_ty = self._fold_ast(env, Kind.TERM, c.ty)
            ev_c = self._fold_ast(env.local_extend(c.name, ev_x), Kind.TYPE, c.c)
            return self._fold(key, [self.model.lam, ev_ty, ev_c])
        elif typ is MetaExp:
            assert False, "NOTE(deh): MetaExp should never be in dataset"
        elif typ is EvarExp:
            ev_exk = self.fold_evar_name(c.exk)
            # NOTE(deh): pruposely leaving out cs
            # ev_cs = self._fold_asts(env, Kind.TYPE, c.cs)
            return self._fold(key, [self.model.evar, ev_exk])
        elif typ is SortExp:
            ev_sort = self.fold_sort_name(c.sort)
            return self._fold(key, [self.model.sort, ev_sort])
        elif typ is CastExp:
            ev_c = self._fold_ast(env, Kind.TERM, c.c)
            ev_ty = self._fold_ast(env, Kind.TYPE, c.ty)
            return self._fold(key, [self.model.cast, ev_c, ev_ty])
        elif typ is LetInExp:
            ev_c1 = self._fold_ast(env, Kind.TERM, c.c1)
            ev_ty = self._fold_ast(env, Kind.TYPE, c.ty)
            ev_c2 = self._fold_ast(env.local_extend(c.name, ev_c1), Kind.TERM, c.c2)
            return self._fold(key, [self.model.letin, ev_c1, ev_ty, ev_c2])
        elif typ is CaseExp:
            ev_ret = self._fold_ast(env, Kind.TERM, c.ret)
            ev_match = self._fold_ast(env, Kind.TERM, c.match)
            ev_cases = self._fold_asts(env, Kind.TERM, c.cases)
            return self._fold(key, [self.model.case, ev_ret, ev_match, *ev_cases])
        elif typ is FixExp:
            # 1. Create initial embeddings
            for name in c.names:
                ev = self.fold_fix_name(name)
                # self.fixbody_embed[name] = ev
                env = env.local_extend(name, ev)

            # 2. Use initial embeddings
            ev_tys = []
            ev_cs = []
            for ty, body in zip(c.tys, c.cs):
                ev_tys += [self._fold_ast(env, Kind.TYPE, ty)]
                ev_c = self._fold_ast(env, Kind.TERM, body)
                # NOTE(deh): Tie the knot appropriately
                # self.fix_embed[name] = ev_c
                ev_cs += [ev_c]
            return self._fold(key, [self.model.fix, *ev_tys, *ev_cs])
        elif typ is CoFixExp:
            # NOTE(deh): CoFixExp not in dataset
            raise NameError("NOTE(deh): CoFixExp not in dataset")
        elif typ is ProjExp:
            # NOTE(deh): ProjExp not in dataset
            raise NameError("NOTE(deh): ProjExp not in dataset")
            # ev = self._fold_ast(env, Kind.TERM, c.c)
            # return self._fold(key, [self.model.proj, ev])
        else:
            raise NameError("Kind {} not supported".format(c))

    def _fold_asts(self, env, kind, cs):
        return [self._fold_ast(env, kind, c) for c in cs]

    # -------------------------------------------
    # Mid-level AST folding

    def _gref_args(self, env, gref):
        ty = type(gref)
        if ty is VarRef:
            try:
                ev_x = env.lookup_id(Name(gref.x))
            except NotFound:
                print("Lookup error at VARREF", gref.x, env)
                ev_x = self.fold_local_var(None)
            return [self.model.gref_var, ev_x]
        elif ty is ConstRef:
            ev_const = self.fold_const_name(gref.const)
            return [self.model.gref_const, ev_const]
        elif ty is IndRef:
            ev_ind = self.fold_ind_name(gref.ind)
            return [self.model.gref_ind, ev_ind]
        elif ty is ConstructRef:
            ev_ind = self.fold_ind_name(gref.ind)
            ev_conid = self.fold_conid_name((gref.ind, gref.conid))
            return [self.model.gref_construct, ev_ind, ev_conid]
        else:
            raise NameError("Gref {} not supported".format(gref))

    def _fold_mid(self, env, gc):
        key = gc.tag
        if self.f_sharing:
            if key in self.folded:
                return self.folded[key]

        ty = type(gc)
        if ty is GRef:
            return self._fold(key, self._gref_args(env, gc.gref))
        elif ty is GVar:
            try:
                ev_x = env.lookup_id(Name(gc.x))
            except NotFound:
                print("Lookup error at GVAR", gc.x, env)
                ev_x = self.fold_local_var(None)
            return self._fold(key, [self.model.gvar, ev_x])
        elif ty is GEvar:
            ev_ek = self.fold_evar_name(gc.ev)
            return self._fold(key, [self.model.gevar, ev_ek])
        elif ty is GPatVar:
            ev_pv = env.lookup_id(gc.pv)
            return self._fold(key, [self.model.gpatvar, ev_pv])
        elif ty is GApp:
            ev_gc = self._fold_mid(env, gc.g)
            ev_gcs = []
            if self.model.f_useiarg:
                # Use implicit args (i.e., use everything)
                for gc_p, iarg in zip(gc.gs, gc.iargs):
                    ev_gcs += [self._fold_mid(env, gc_p)]
            else:
                # Omit implicit args
                for gc_p, iarg in zip(gc.gs, gc.iargs):
                    if iarg is None:
                        ev_gcs += [self._fold_mid(env, gc_p)]

            return self._fold(key, [self.model.gapp, ev_gc, *ev_gcs])
        elif ty is GLambda:
            ev_x = self.fold_local_var(gc.g_ty)
            ev_ty = self._fold_mid(env, gc.g_ty)
            ev_c = self._fold_mid(env.local_extend(gc.name, ev_x), gc.g_bod)
            return self._fold(key, [self.model.glambda, ev_ty, ev_c])
        elif ty is GProd:
            ev_x = self.fold_local_var(gc.g_ty)
            ev_ty = self._fold_mid(env, gc.g_ty)
            ev_c = self._fold_mid(env.local_extend(gc.name, ev_x), gc.g_bod)
            return self._fold(key, [self.model.gprod, ev_ty, ev_c])
        elif ty is GLetIn:
            ev_g1 = self._fold_mid(env, gc.g1)
            ev_g2 = self._fold_mid(env.local_extend(gc.name, ev_g1), gc.g2)
            return self._fold(key, [self.model.gletin, ev_g1, ev_g2])
        elif ty is GCases:
            ccs = []
            for cc in gc.ccs:
                for cp in cc.cps:
                    for name in cp.get_names():
                        ev_x = self.fold_local_var(None)
                        env = env.local_extend(name, ev_x)
                ccs += [self._fold_mid(env, cc.g)]
            return self._fold(key, [self.model.gcases, *ccs])
        elif ty is GLetTuple:
            ev_g1_fst = self._fold_mid(env, gc.g1_fst)
            ev_g1_snd = self._fold_mid(env, gc.g1_snd)
            ev_g2 = self._fold_mid(env.local_extend(gc.names[0], ev_g1_fst).local_extend(gc.names[1], ev_g1_snd), gc.g2)
            return self._fold(key, [self.model.glettuple, ev_g2])
        elif ty is GIf:
            ev_g1 = self._fold_mid(env, gc.g1)
            ev_g2 = self._fold_mid(env, gc.g2)
            ev_g3 = self._fold_mid(env, gc.g3)
            return self._fold(key, [self.model.gif, ev_g1, ev_g2, ev_g3])
        elif ty is GRec:
            # 1. Create initial embeddings
            for ident in gc.ids:
                ev = self.fold_fix_name(ident)
                env = env.local_extend(Name(ident), ev)

            # 2. Add in glog_decl arguments
            for gdecls in gc.gdeclss:
                for gdecl in gdecls:
                    ev_foo = self._fold_mid(env, gdecl.gc)
                    env = env.local_extend(gdecl.name, ev_foo)

            # 3. Use initial embeddings
            ev_tys = []
            ev_bods = []
            for ty, body in zip(gc.gc_tys, gc.gc_bods):
                ev_tys += [self._fold_mid(env, ty)]
                ev_bods += [self._fold_mid(env, body)]
            return self._fold(key, [self.model.grec, *ev_tys, *ev_bods])
        elif ty is GSort:
            ev_sort = self.fold_sort_name(gc.gsort)
            return self._fold(key, [self.model.gsort, ev_sort])
        elif ty is GHole:
            raise NameError("Not in dataset")
        elif ty is GCast:
            ev_g = self._fold_mid(env, gc.g)
            return self._fold(key, [self.model.gcast, ev_g])
        else:
            raise NameError("Kind {} not supported".format(gc))

    def _fold_mids(self, env, gcs):
        return [self._fold_mid(env, x) for x in gcs]

    # -------------------------------------------
    # Global constant folding
    def lookup(self, lt):
        return self.folder.add('embed_lookup_f', autograd.Variable(self.torch.LongTensor([lt])))

    def fold_evar_name(self, exk):
        """Override Me"""
        ident = self.model.fix_id('evar', self.model.evar_to_idx[exk])
        return self.lookup(ident)

    def fold_const_name(self, const):
        """Override Me"""
        ident = self.model.fix_id('const', self.model.const_to_idx[const])
        return self.lookup(ident)

    def fold_sort_name(self, sort):
        """Override Me"""
        ident = self.model.fix_id('sort', self.model.sort_to_idx[sort])
        return self.lookup(ident)

    def fold_ind_name(self, ind):
        """Override Me"""
        ident = self.model.fix_id('ind', self.model.ind_to_idx[ind.mutind])
        return self.lookup(ident)

    def fold_conid_name(self, ind_and_conid):
        """Override Me"""
        ind, conid = ind_and_conid
        ident = self.model.fix_id('conid', self.model.conid_to_idx[(ind.mutind, conid)])
        return self.lookup(ident)

    def fold_fix_name(self, name):
        """Override Me"""
        ident = self.model.fix_id('fix', self.model.fix_to_idx[name])
        return self.lookup(ident)

    # -------------------------------------------
    # Local variable folding

    def fold_local_var(self, ty):
        """Override Me"""
        return self.folder.add('var_normal', self.torch.FloatTensor(1, self.model.init_D))


class TreeLSTM(nn.Module):
    def __init__(self, state, weight_dropout, variational):
        super().__init__()
        self.whx = nn.Linear(state * 2, state * 5)
        if weight_dropout:
            self.whx = WeightDrop(self.whx, ["weight"], weight_dropout, variational)

    def forward(self, right_h, right_c, left_h, left_c):
        # takes x as first arg, h as second
        a, i, f1, f2, o = self.whx(torch.cat([left_h, right_h], dim=-1)).chunk(5, -1)
        c = (a.tanh() * i.sigmoid() + f1.sigmoid() * left_c + f2.sigmoid() * right_c)
        h = o.sigmoid() * c.tanh()
        return h, c


# -------------------------------------------------
# Model

class LinearModel(nn.Module):
    def __init__(self, outsize=3, f_mid=False, f_useiarg=True, f_useedit=False):
        super().__init__()
        self.outsize = 3
        self.f_mid = f_mid
        self.f_useiarg = f_useiarg
        self.f_useedit = f_useedit

        if not self.f_mid:
            # Kern level
            self.typ = "kern"
        elif self.f_useiarg:
            # Midlevel with Imp args
            self.typ = "mid"
        else:
            # Midlvl with No imp args
            self.typ = "mid_noimp"

        # Features
        size_features = ['%s_concl_size' % self.typ, '%s_ctx_size' % self.typ]
        len_features = ['len_ctx']
        edit_dist_features = []
        if f_useedit:
            edit_dist_features = ['%s_str_dists' % self.typ]
        features = size_features + len_features + edit_dist_features
        insize = len(features)

        def _get_features(pt, features=features):
            return [getattr(pt, f) for f in features]

        self.get_features = _get_features

        # Model
        self.f_linear = True
        self.pred = nn.Linear(insize, outsize)


class TacStModel(nn.Module):
    def __init__(self, sort_to_idx, const_to_idx, ind_to_idx, conid_to_idx, evar_to_idx, fix_to_idx,
                 D=128, state=128, outsize=3, eps=1e-6, ln=False, treelstm=False, lstm=False, dropout=0.0,
                 attention=False, heads=1, weight_dropout=0.0, variational=False, conclu_pos=0,
                 f_mid=False, f_useiarg=True):
        super().__init__()

        # Dimensions
        self.D = D            # Dimension of embeddings
        # Dimension of GRU/LSTM/TreeLSTM state.
        # For LSTM's, each of hidden state and cell state has that size
        self.state = state
        self.outsize = outsize
        self.f_mid = f_mid           # Use mid-level AST (as opposed to kernel-level AST)
        self.f_useiarg = f_useiarg   # Use implicit arguments

        # Model
        self.f_linear = False
        table_names = ['sort', 'const', 'ind', 'conid', 'evar', 'fix', 'fixbody']
        tables = [sort_to_idx, const_to_idx, ind_to_idx, conid_to_idx, evar_to_idx, fix_to_idx, fix_to_idx]
        shift = 0
        self.shifts = {}
        for table_name, table in zip(table_names, tables):
            self.shifts[table_name] = shift
            shift += len(table)
        self.treelstm = treelstm
        self.lstm = lstm

        # So, we have hidden, state; instead of just state
        self.tup = self.treelstm or self.lstm

        if self.tup:
            # Initial state
            self.init_D = 2*D
            self.init_state = 2*state
        else:
            self.init_D = D
            self.init_state = state

        self.embed_table = nn.Embedding(shift, self.init_D)

        # Embeddings for constants
        self.sort_to_idx = sort_to_idx
        self.const_to_idx = const_to_idx
        self.ind_to_idx = ind_to_idx
        self.conid_to_idx = conid_to_idx
        self.evar_to_idx = evar_to_idx
        self.fix_to_idx = fix_to_idx

        if self.f_mid:
            attrs = ["gref_var", "gref_const", "gref_ind", "gref_construct", "gvar", "gevar", "gpatvar", "gapp",
                     "glambda", "gprod", "gletin", "gcases", "glettuple", "gif", "grec", "gsort", "gcast"]
        else:
            attrs = ["rel", "var", "evar", "sort", "cast", "prod", "lam", "letin", "app", "const", "ind", "construct",
                     "case", "fix", "cofix", "proj1"]
        for attr in attrs:
            self.__setattr__(attr, nn.Parameter(torch.randn(1, self.init_state)))

        # Conclusion position
        self.conclu_pos = conclu_pos

        # Sequence models
        seq_args = {'get_hiddens': False, 'ln': ln, 'input_dropout': dropout > 0.0, 'conclu_pos': self.conclu_pos}
        if seq_args['ln']:
            # Layer Norm
            self.ast_gamma = nn.Parameter(torch.ones(self.init_state))
            self.ast_beta = nn.Parameter(torch.zeros(self.init_state))
            self.ctx_gamma = nn.Parameter(torch.ones(self.init_state))
            self.ctx_beta = nn.Parameter(torch.zeros(self.init_state))
            self.eps = eps

        if seq_args['input_dropout']:
            # Input droput
            self.input_dropout = nn.Dropout(dropout)

        if attention:
            self.m = heads
            self.attn_sv_init = nn.Parameter(torch.zeros(1, heads*state))
            self.attn_q = nn.Linear(state, heads*state)
            self.attn_kv = nn.Linear(state, 2*heads*state)

        self.ast_cell_init_state = nn.Parameter(torch.randn(1, self.init_state))
        self.ctx_cell_init_state = nn.Parameter(torch.randn(1, self.init_state))

        if self.treelstm:
            self.ast_cell = TreeLSTM(state, weight_dropout, variational)
            self.ast_emb_func = lambda folder, xs: seq_embed('ast_tree', folder, xs, self.ast_cell_init_state, **seq_args)
            self.ctx_cell = TreeLSTM(state, weight_dropout, variational)
            self.ctx_emb_func = lambda folder, xs: seq_embed('ctx_tree', folder, xs, self.ctx_cell_init_state, **seq_args)
        else:
            if self.lstm:
                self.ast_cell = nn.LSTMCell(state, state)
                self.ctx_cell = nn.LSTMCell(state, state)
                name = "_lstm"
            else:
                # Default is GRU
                self.ast_cell = nn.GRUCell(state, state)
                self.ctx_cell = nn.GRUCell(state, state)
                name = ""
            self.ast_emb_func = lambda folder, xs: seq_embed('ast' + name, folder, xs, self.ast_cell_init_state, **seq_args)
            if not attention:
                self.ctx_emb_func = lambda folder, xs: seq_embed('ctx' + name, folder, xs, self.ctx_cell_init_state, **seq_args)
            else:
                self.ctx_emb_func = lambda folder, xs: seq_sigmoid_attn_embed(folder, xs, self.attn_sv_init, **seq_args)

        if weight_dropout and not treelstm:
            weights = ["weight_hh"]
            self.ast_cell = WeightDrop(self.ast_cell, weights=weights, dropout=weight_dropout, variational=variational)
            self.ctx_cell = WeightDrop(self.ctx_cell, weights=weights, dropout=weight_dropout, variational=variational)
        self.pred = self.ctx_func
        self.proj = nn.Linear(state + 1, state)
        self.final = nn.Linear(heads*state, outsize)

        # Extra vars
        self.register_buffer('concl_id', torch.ones([1, 1]))
        self.register_buffer('state_id', torch.zeros([1, 1]))

    # Folder forward functions
    def attn_identity(self, x):
        return x

    def attn_q_f(self, x):
        if self.tup:
            # Only use hidden state
            x = x.chunk(2, -1)[0]
        return self.attn_q(x)

    def attn_sv_f(self, q, x, sv):
        if self.tup:
            # Only use hidden state
            x = x.chunk(2, -1)[0]
        batch, state = x.shape
        # q is [b, m*state], x is [b, state], k,v will be [b, m*state]
        k, v = self.attn_kv(x).chunk(2, -1)
        if self.m == 1:
            k = k.unsqueeze(1)
            q = q.unsqueeze(2)
            prod = torch.bmm(k, q).view(batch, 1)/float(np.sqrt(state))
            prsg = prod.sigmoid()
            sv = sv + (prsg * v)
        else:
            k = k.contiguous().view(batch*self.m, 1, state)  # or torch.stack(k.chunk(self.m,-1),0)
            q = q.contiguous().view(batch*self.m, state, 1)
            v = v.contiguous().view(batch*self.m, state)
            prod = torch.bmm(k, q).view(batch*self.m, 1) / float(np.sqrt(state))
            prsg = prod.sigmoid()
            sv = sv + (prsg * v).view(batch, self.m*state)
        return sv

    def input_dropout_f(self, x):
        return self.input_dropout(x)

    def var_normal(self, x):
        return autograd.Variable(x.normal_(), requires_grad=False)

    def identity(self, x):
        return x

    def tup_identity(self, *args):
        return args

    def fix_id(self, table_name, ident):
        return self.shifts[table_name] + ident

    def embed_lookup_f(self, ident):
        return self.embed_table(ident)

    def ast_cell_f(self, x, hidden):
        hidden = self.ast_cell(x, hidden)
        return hidden

    def ctx_cell_f(self, x, hidden):
        hidden = self.ctx_cell(x, hidden)
        return hidden

    def ast_lstm_cell_f(self, right, left):
        right_h, right_c = right.chunk(2, -1)
        left_h, left_c = left.chunk(2, -1)
        hidden = torch.cat(self.ast_cell(right_h, (left_h, left_c)), -1)
        return hidden

    def ctx_lstm_cell_f(self, right, left):
        right_h, right_c = right.chunk(2, -1)
        left_h, left_c = left.chunk(2, -1)
        hidden = torch.cat(self.ctx_cell(right_h, (left_h, left_c)), -1)
        return hidden

    def ast_tree_cell_f(self, right, left):
        right_h, right_c = right.chunk(2, -1)
        left_h, left_c = left.chunk(2, -1)
        out = torch.cat(self.ast_cell(right_h, right_c, left_h, left_c), -1)
        return out

    def ctx_tree_cell_f(self, right, left):
        right_h, right_c = right.chunk(2, -1)
        left_h, left_c = left.chunk(2, -1)
        out = torch.cat(self.ctx_cell(right_h, right_c, left_h, left_c), -1)
        return out

    def ast_ln_f(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.ast_gamma * (x - mean) / (std + self.eps) + self.ast_beta

    def ctx_ln_f(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.ctx_gamma * (x - mean) / (std + self.eps) + self.ctx_beta

    def final_f(self, x):
        if self.tup:
            # Only apply final to hidden
            x = x.chunk(2, -1)[0]
        return self.final(x)

    def proj_f(self, *xs):
        if self.tup:
            # Only apply proj to hidden
            x, cell = xs[0].chunk(2, -1)
            x = torch.cat([x, xs[1]], dim=-1)
            x = self.proj(x)
            return torch.cat([x, cell], dim=-1)
        else:
            x = torch.cat(xs, dim=-1)
            return self.proj(x)

    # Folder helper functions, call the forward functions
    def mask(self, folder, xs):
        # First element is conclu, rest is state
        projs = []
        for i, x in enumerate(xs):
            if i == 0:
                ident = self.concl_id
            else:
                ident = self.state_id
            projs.append(folder.add('proj_f', x, autograd.Variable(ident)))
        return projs

    def ctx_func(self, folder, *tacst_evs):
        xs = self.mask(folder, tacst_evs)
        if self.conclu_pos != 0:
            # moved to end
            xs = xs[1:] + xs[0:1]
        x = self.ctx_emb_func(folder, xs)
        # Final layer for logits
        pred1 = folder.add('final_f', x)
        return pred1


class WeightDrop(torch.nn.Module):
    def __init__(self, nnmodule, weights, dropout=0.0, variational=False):
        super(WeightDrop, self).__init__()
        self.nnmodule = nnmodule
        self.weights = weights
        self.dropout = dropout
        self.variational = variational
        self._setup()

    def widget_demagnetizer_y2k_edition(*args, **kwargs):
        # We need to replace flatten_parameters with a nothing function
        # It must be a function rather than a lambda as otherwise pickling explodes
        # We can't write boring code though, so ... WIDGET DEMAGNETIZER Y2K EDITION!
        # (╯°□°）╯︵ ┻━┻
        return

    def _setup(self):
        # Terrible temporary solution to an issue regarding compacting weights re: CUDNN RNN
        if issubclass(type(self.nnmodule), torch.nn.RNNBase):
            self.nnmodule.flatten_parameters = self.widget_demagnetizer_y2k_edition

        for name_w in self.weights:
            print('Applying weight drop of {} to {}'.format(self.dropout, name_w))
            w = getattr(self.nnmodule, name_w)
            del self.nnmodule._parameters[name_w]
            self.nnmodule.register_parameter(name_w + '_raw', nn.Parameter(w.data))

    def _setweights(self):
        for name_w in self.weights:
            raw_w = getattr(self.nnmodule, name_w + '_raw')
            if self.variational:
                mask = torch.autograd.Variable(torch.ones(raw_w.size(0), 1))
                if raw_w.is_cuda:
                    mask = mask.cuda()
                mask = torch.nn.functional.dropout(mask, p=self.dropout, training=True)
                w = mask.expand_as(raw_w) * raw_w
            else:
                w = torch.nn.functional.dropout(raw_w, p=self.dropout, training=self.training)
            setattr(self.nnmodule, name_w, w)

    def forward(self, *args):
        self._setweights()
        return self.nnmodule.forward(*args)
