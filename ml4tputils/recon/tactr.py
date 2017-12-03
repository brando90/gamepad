import json
import networkx as nx
import numpy as np

from coq.ast import Name
from coq.interp import InterpCBName, SizeCoqVal
from coq.util import ChkCoqExp, SizeCoqExp, HistCoqExp, COQEXP_HIST
from lib.myenv import MyEnv
from lib.myhist import MyHist
from lib.myutil import dict_ls_app
from recon.parse_tacst import TacKind
import recon.build_tactr

"""
[Note]

A reconstructed tactic tree. Contains methods for computing statistics.
"""


# -------------------------------------------------
# Tactics

TACTICS = ["<coretactics::intro@0>",
           "<coretactics::assumption@0>",
           "<coretactics::clear@0>",
           "<coretactics::clearbody@0>",
           "<coretactics::constructor@0>",
           "<coretactics::constructor@1>",
           "<coretactics::exact@0>",
           "<coretactics::exists@1>",
           "<coretactics::left@0>",
           "<coretactics::right@0>",
           "<coretactics::right_with@0>",
           "<coretactics::split@0>",
           "<coretactics::symmetry@0>",
           "<coretactics::transitivity@0>",
           "<extratactics::contradiction@0>",
           "<extratactics::discriminate@0>",
           "<g_auto::auto@0>",
           "<g_auto::eauto@0>",
           "<g_auto::trivial@0>",
           "<ssreflect_plugin::ssrapply@0>",
           "<ssreflect_plugin::ssrapply@1>",
           "<ssreflect_plugin::ssrcase@0>",
           "<ssreflect_plugin::ssrcase@1>",
           "<ssreflect_plugin::ssrclear@0>",
           "<ssreflect_plugin::ssrcongr@0>",
           "<ssreflect_plugin::ssrelim@0>",
           "<ssreflect_plugin::ssrexact@0>",
           "<ssreflect_plugin::ssrexact@1>",
           "<ssreflect_plugin::ssrhave@0>",
           "<ssreflect_plugin::ssrmove@0>",
           "<ssreflect_plugin::ssrmove@1>",
           "<ssreflect_plugin::ssrmove@2>",
           "<ssreflect_plugin::ssrpose@0>",
           "<ssreflect_plugin::ssrpose@2>",
           "<ssreflect_plugin::ssrrewrite@0>",
           "<ssreflect_plugin::ssrset@0>",
           "<ssreflect_plugin::ssrsuff@0>",
           "<ssreflect_plugin::ssrsuffices@0>",
           "<ssreflect_plugin::ssrtclby@0>",
           "<ssreflect_plugin::ssrtcldo@0>",
           "<ssreflect_plugin::ssrtclintros@0>",
           "<ssreflect_plugin::ssrtclseq@0>",
           "<ssreflect_plugin::ssrwithoutloss@0>",
           "<ssreflect_plugin::ssrwithoutlossss@0>",
           "<ssreflect_plugin::ssrwlog@0>",
           "<ssreflect_plugin::ssrwlogss@0>",
           "<ssreflect_plugin::ssrwlogs@0>"
           ]


TACTIC_HIST = MyHist(TACTICS)


# -------------------------------------------------
# Tactic Tree

class TacTree(object):
    def __init__(self, name, edges, graph, tacst_info, gid_tactic, decoder):
        # Input
        self.name = name               # Lemma name
        self.edges = edges             # [TacEdge]
        self.graph = graph             # nx.MultDiGraph[TacStId, TacStId]
        self.tacst_info = tacst_info   # Dict[gid, (ctx, goal, ctx_e, goal_e)]
        self.gid_tactic = gid_tactic   # Dict[int, TacEdge]
        self.decoder = decoder         # Decode asts
        self.chk = ChkCoqExp(decoder.concr_ast)
        self.chk.chk_concr_ast()
        self.sce_full = SizeCoqExp(decoder.concr_ast, f_shared=False)
        self.sce_sh = SizeCoqExp(decoder.concr_ast, f_shared=True)
        self.hce = HistCoqExp(decoder.concr_ast)

        self.notok = []

        # Root, error/terminal states, and create flattened view
        self._root()
        assert self.root
        self._goals()
        self._term_goals()
        self._err_goals()
        self._tactics()
        self._flatten_view()

    def _root(self):
        self.root = None
        for node in self.graph.nodes():
            self_edges = 0
            for edge in self.graph.in_edges(node):
                if edge[0] == edge[1]:
                    self_edges += 1
            if self.graph.in_degree(node) == self_edges:
                self.root = node
                break

    def _goals(self):
        self.goals = self.graph.nodes()

    def _err_goals(self):
        self.err_goals = []
        for edge in self.edges:
            if edge.conn2err():
                self.err_goals += [edge.tgt]

    def _term_goals(self):
        self.term_goals = []
        for edge in self.edges:
            if edge.conn2term():
                self.term_goals += [edge.tgt]

    def _tactics(self):
        self.tactics = {}
        for edge in self.edges:
            if edge.tid in self.tactics:
                self.tactics[edge.tid] += [edge]
            else:
                self.tactics[edge.tid] = [edge]

    def _flatten_view(self):
        self.flatview = []
        seen = set()
        for edge in self.edges:
            try:
                depth = len(nx.algorithms.shortest_path(self.graph, self.root, edge.tgt))
                if edge.tid not in seen:
                    if edge.tgt.gid in self.tacst_info:
                        ctx, goal, ctx_e, goal_e = self.tacst_info[edge.tgt.gid]
                        self.flatview += [(depth, edge.tgt, ctx, goal, ctx_e, goal_e, edge)]
                    elif edge.conn2err() or edge.conn2term():
                        # print("DOING", edge.src, self.tacst_info)
                        ctx, goal, ctx_e, goal_e = self.tacst_info[edge.src.gid]
                        self.flatview += [(depth, edge.tgt, ctx, goal, ctx_e, goal_e, edge)]
            except nx.exception.NetworkXNoPath:
                pass
            seen.add(edge.tid)

    def in_edge(self, gid):
        gids = list(self.graph.predecessors(gid))
        acc = []
        for edge in self.edges:
            if edge.src in gids and edge.tgt == gid:
                acc += [edge]
        return acc

    def out_edges(self, gid):
        gids = list(self.graph.successors(gid))
        acc = []
        for edge in self.edges:
            if edge.tgt in gids and edge.src == gid:
                acc += [edge]
        return acc

    def unique_const(self):
        return self.hce.unique_const

    def unique_ind(self):
        return self.hce.unique_ind

    def unique_conid(self):
        return self.hce.unique_conid

    def _traverse_info(self, ordered_gids):
        acc = []
        for src_gid, tgt_gid in ordered_gids:
            if src_gid in self.tacst_info:
                pp_ctx, pp_goal, ctx_ids, goal_idx = self.tacst_info[src_gid]
                acc += [('OPEN', src_gid, pp_ctx, pp_goal, ctx_ids,
                         goal_idx, self.gid_tactic[src_gid])]
            elif (recon.build_tactr.is_err(tgt_gid) or
                  recon.build_tactr.is_term(tgt_gid)):
                acc += [('STOP', tgt_gid, self.gid_tactic[tgt_gid])]
        return acc

    def bfs_traverse(self):
        bfs = list(nx.bfs_edges(self.graph, self.root))
        return self._traverse_info(bfs)

    def dfs_traverse(self):
        dfs = list(nx.dfs_edges(self.graph, source=self.root))
        return self._traverse_info(dfs)

    def view_err_paths(self):
        acc = []
        for egid in self.err_goals:
            try:
                acc += [nx.algorithms.shortest_path(self.graph, self.root, egid)]
            except nx.exception.NetworkXNoPath:
                self.notok += [str(egid)]
        return acc

    def view_term_paths(self):
        acc = []
        for tgid in self.term_goals:
            try:
                acc += [nx.algorithms.shortest_path(self.graph, self.root, tgid)]
            except nx.exception.NetworkXNoPath:
                self.notok += [str(tgid)]
        return acc

    def view_have_info(self):
        acc = []
        for edge in self.edges:
            if edge.name.startswith("<ssreflect_plugin::ssrhave@0>") and \
               edge.isbod:
                path = []
                for tgid in self.term_goals:
                    try:
                        path = nx.algorithms.shortest_path(self.graph, edge.src, tgid)
                        break
                    except nx.exception.NetworkXNoPath:
                        pass
                acc += [(edge.ftac, len(edge.ftac), path)]
        return acc

    def view_tactic_hist(self, f_compress=False):
        hist = TACTIC_HIST.empty()
        for k, tacs in self.tactics.items():
            tac = tacs[0]
            if tac.tkind == TacKind.ML:
                tac_name = tac.name.split()[0]
                hist = TACTIC_HIST.inc_insert(hist, tac_name, 1)
        if f_compress:
            return [v for _, v in TACTIC_HIST.view(hist)]
        else:
            return TACTIC_HIST.view(hist)

    def view_depth_ctx_items(self):
        hist = {}
        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            if ctx:
                v = len(ctx_e)
            else:
                v = 0
            dict_ls_app(hist, depth, v)
        return hist

    def view_depth_ctx_size(self):
        """Returns Dict[depth, [total string typ size]]"""
        hist = {}
        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            if ctx:
                v = np.sum([len(ty) for _, ty in ctx.items()])
            else:
                v = 0
            dict_ls_app(hist, depth, v)
        return hist

    def view_depth_goal_size(self):
        """Returns Dict[depth, [string typ size]]"""
        hist = {}
        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            dict_ls_app(hist, depth, len(goal))
        return hist

    def view_depth_astctx_size(self):
        """Returns Dict[depth, [total ast typ size]]"""
        hist = {}
        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            if ctx_e:
                ls = []
                for ident in ctx_e:
                    # print(ctx_e, self.decoder.typs_table)
                    key = self.decoder.typs_table[ident]
                    size = self.sce_full.decode_size(key)
                    ls += [size]
                v = np.sum(ls)
            else:
                v = 0
            dict_ls_app(hist, depth, v)
        return hist

    def view_depth_astgoal_size(self):
        """Returns Dict[depth, [total ast typ size]]"""
        hist = {}
        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            dict_ls_app(hist, depth, self.sce_full.decode_size(goal_e))
        return hist

    def view_depth_tactic_hist(self):
        max_depth = max([depth for depth, _, _, _, _, _, _ in self.flatview])
        hist = {}
        for depth in range(max_depth + 1):
            hist[depth] = TACTIC_HIST.empty()

        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            TACTIC_HIST.inc_insert(hist[depth], tac.name, 1)
        return hist

    def hist_coqexp(self):
        hists = [self.hce.decode_hist(edx) for ident, edx in
                 self.decoder.typs_table.items()]

        acc = []
        seen = set()
        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            if goal_e not in seen:
                acc += [self.hce.decode_hist(goal_e)]
                seen.add(goal_e)
        return COQEXP_HIST.merges(hists + acc)

    def view_comp(self):
        vals = {}
        static_full_comp = {}
        static_sh_comp = {}
        cbname_comp = {}
        scv = SizeCoqVal(self.decoder.concr_ast)
        for depth, gid, ctx, goal, ctx_e, goal_e, tac in self.flatview:
            env = MyEnv()
            for ident in ctx_e:
                if ident in vals:
                    v = vals[ident]
                else:
                    edx = self.decoder.typs_table[ident]
                    c = self.decoder.decode_exp_by_key(edx)
                    cbname = InterpCBName()
                    v = cbname.interp(env, c)
                    vals[ident] = v
                    edx = self.decoder.typs_table[ident]
                    static_full_comp[ident] = self.sce_full.decode_size(edx)
                    static_sh_comp[ident] = self.sce_sh.decode_size(edx)
                    cbname_comp[ident] = scv.size(v)
                env = env.extend(Name(ident), v)
        return static_full_comp, static_sh_comp, cbname_comp

    def stats(self):
        term_path_lens = [len(path) for path in self.view_term_paths()]
        err_path_lens = [len(path) for path in self.view_err_paths()]
        """
        avg_depth_ctx_items = [(k, np.mean(v)) for k, v in
                               self.view_depth_ctx_items().items()]
        avg_depth_ctx_size = [(k, np.mean(v)) for k, v in
                              self.view_depth_ctx_size().items()]
        avg_depth_goal_size = [(k, np.mean(tysz)) for k, tysz in
                               self.view_depth_goal_size().items()]
        avg_depth_astctx_size = [(k, np.mean(v)) for k, v in
                                 self.view_depth_astctx_size().items()]
        avg_depth_astgoal_size = [(k, np.mean(tysz)) for k, tysz in
                                  self.view_depth_astgoal_size().items()]
        """
        #static_full_comp, static_sh_comp, cbname_comp = self.view_comp()
        info = {'hist': self.view_tactic_hist(f_compress=True),
                'num_tacs': len(self.tactics),
                'num_goals': len(self.goals),
                'num_term': len(self.term_goals),
                'num_err': len(self.err_goals),
                'term_path_lens': term_path_lens,
                'err_path_lens': err_path_lens,
                #'have_info': self.view_have_info(),
                #'avg_depth_ctx_items': avg_depth_ctx_items,
                #'avg_depth_ctx_size': avg_depth_ctx_size,
                #'avg_depth_goal_size': avg_depth_goal_size,
                #'avg_depth_astctx_size': avg_depth_astctx_size,
                #'avg_depth_astgoal_size': avg_depth_astgoal_size,
                #'hist_coqexp': self.hist_coqexp(),
                #'static_full_comp': [v for _, v in static_full_comp.items()],
                #'static_sh_comp': [v for _, v in static_sh_comp.items()],
                #'cbname_comp': [v for _, v in cbname_comp.items()],
                'notok': self.notok}
        return info

    def log_stats(self, h_file):
        info = self.stats()
        msg = json.dumps({"lemma": self.name, "info": info}, cls=recon.build_tactr.TacStIdEncoder)
        h_file.write(msg)
        h_file.write("\n")
        return info

    def dump(self):
        print(">>>>>>>>>>>>>>>>>>>>")
        print("Root:", self.root)
        print("Goals: ", "[{}]".format(",".join([str(g) for g in self.goals])))
        print("Tactics:", self.tactics)
        for gid in self.goals:
            s1 = ", ".join([str(x) for x in self.in_edge(gid)])
            print("In edge for {}:".format(gid), s1)
            s2 = ", ".join([str(x) for x in self.out_edges(gid)])
            print("Out edges for {}:".format(gid), s2)
        print("Terminal states:", "[{}]".format(",".join([str(g) for g in self.term_goals])))
        print("Error states:", "[{}]".format(",".join([str(g) for g in self.err_goals])))
        print("Terminal path lengths:", self.view_term_paths())
        print("Error path lengths:", self.view_err_paths())
        print("<<<<<<<<<<<<<<<<<<<<")