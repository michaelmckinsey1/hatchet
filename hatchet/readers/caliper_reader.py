# Copyright 2017-2023 Lawrence Livermore National Security, LLC and other
# Hatchet Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: MIT

import json
import re
import subprocess
import os
import math
import warnings

import pandas as pd
import numpy as np

import hatchet.graphframe
from hatchet.node import Node
from hatchet.graph import Graph
from hatchet.frame import Frame
from hatchet.util.timer import Timer
from hatchet.util.executable import which

unknown_label_counter = 0


class CaliperReader:
    """Read in a Caliper file (`cali` or split JSON) or file-like object."""

    def __init__(self, filename_or_stream, query="", node_ordering=True):
        """Read from Caliper files (`cali` or split JSON).

        Args:
            filename_or_stream (str or file-like): name of a `cali` or
                `cali-query` split JSON file, OR an open file object
            query (str): cali-query arguments (for cali file)
            node_ordering (bool): use node ordering (default: true)
        """
        self.filename_or_stream = filename_or_stream
        self.filename_ext = ""
        self.query = query
        self.node_ordering = node_ordering

        self.json_data = {}
        self.json_cols = {}
        self.json_cols_mdata = {}
        self.json_nodes = {}

        self.metadata = {}

        self.idx_to_label = {}
        self.idx_to_node = {}
        self.node_order = {}

        self.timer = Timer()
        self.nid_col_name = "nid"

        if isinstance(self.filename_or_stream, str):
            _, self.filename_ext = os.path.splitext(filename_or_stream)

    def read_json_sections(self):
        # if cali-query exists, extract data from .cali to a file-like object
        if self.filename_ext == ".cali":
            cali_query = which("cali-query")
            if not cali_query:
                raise ValueError("from_caliper() needs cali-query to query .cali file")
            cali_json = subprocess.Popen(
                [cali_query, "-q", self.query, self.filename_or_stream],
                stdout=subprocess.PIPE,
            )
            self.filename_or_stream = cali_json.stdout

        # if filename_or_stream is a str, then open the file, otherwise
        # directly load the file-like object
        if isinstance(self.filename_or_stream, str):
            with open(self.filename_or_stream) as cali_json:
                json_obj = json.load(cali_json)
        else:
            json_obj = json.loads(self.filename_or_stream.read().decode("utf-8"))

        # read various sections of the Caliper JSON file
        self.json_data = json_obj["data"]
        self.json_cols = json_obj["columns"]
        self.json_cols_mdata = json_obj["column_metadata"]
        self.json_nodes = json_obj["nodes"]

        # read run metadata: all top-level elements in the json object that aren't
        # one of the above sections are run metadata
        skip = ["data", "columns", "column_metadata", "nodes"]
        keys = [k for k in json_obj.keys() if k not in skip]
        self.metadata = {k: json_obj[k] for k in keys}

        # decide which column to use as the primary path hierarchy
        # first preference to callpath if available
        if "source.function#callpath.address" in self.json_cols:
            self.path_col_name = "source.function#callpath.address"
            self.node_type = "function"
            if "path" in self.json_cols:
                self.both_hierarchies = True
            else:
                self.both_hierarchies = False
        elif "path" in self.json_cols:
            self.path_col_name = "path"
            self.node_type = "region"
            self.both_hierarchies = False
        else:
            raise ValueError(
                "No hierarchy column in the Caliper data (found columns: {}). "
                "Hatchet needs a 'path' column (region annotations) or a "
                "'source.function#callpath.address' column (call-path "
                "sampling). When querying a .cali file, select and group by "
                "'path' ('select path,... group by path format json-split'; "
                "cali-query older than 2.10 needs 'prop:nested' instead of "
                "'path'): since Caliper 2.10, function annotations are "
                "recorded as 'region', so queries that group by 'function' "
                "find no hierarchy in newer files.".format(self.json_cols)
            )

        # remove data entries containing None in `path` column (null in json file)
        # first, get column where `path` data is
        # then, parse json_data list of lists to identify lists containing None in
        # `path` column
        path_col = self.json_cols.index(self.path_col_name)
        entries_to_remove = []
        for sublist in self.json_data:
            if sublist[path_col] is None:
                entries_to_remove.append(sublist)
        # then, remove them from the json_data list
        for i in entries_to_remove:
            self.json_data.remove(i)

        # change column names
        for idx, item in enumerate(self.json_cols):
            if item == self.path_col_name:
                # this column is just a pointer into the nodes section
                self.json_cols[idx] = self.nid_col_name
            # make other columns consistent with other readers
            if item == "mpi.rank":
                self.json_cols[idx] = "rank"
            if item == "module#cali.sampler.pc":
                self.json_cols[idx] = "module"
            if item == "sum#time.duration" or item == "sum#avg#sum#time.duration":
                self.json_cols[idx] = "time"
            if (
                item == "inclusive#sum#time.duration"
                or item == "sum#avg#inclusive#sum#time.duration"
            ):
                self.json_cols[idx] = "time (inc)"

        # make list of metric columns ("Node order" is bookkeeping, not a metric)
        self.metric_columns = []
        for idx, item in enumerate(self.json_cols_mdata):
            if (
                self.json_cols[idx] not in ("rank", "Node order")
                and item["is_value"] is True
            ):
                self.metric_columns.append(self.json_cols[idx])

        # Caliper (>= 2.10) can record the order in which regions were first
        # entered (min(aggregate.slot), aliased "Node order"). Only use it if
        # the data actually contains it; otherwise fall back to the default
        # (alphabetical) ordering instead of failing.
        self.node_order = {}
        if self.node_ordering:
            if "Node order" in self.json_cols:
                self.node_order = self._read_node_order()
            if not self.node_order:
                warnings.warn(
                    "node_ordering=True, but the Caliper data has no 'Node "
                    "order' column, so nodes are ordered alphabetically. To keep "
                    "call order, select it as 'Node order', e.g. add "
                    "'min(aggregate.slot) as \"Node order\"' to the cali-query "
                    "query for raw data recorded with Caliper >= 2.10; pass "
                    "node_ordering=False to silence this warning."
                )
                self.node_ordering = False

    def _read_node_order(self):
        """Map each hierarchy node index to its Caliper "Node order" value.

        The column is looked up by name (its position depends on the query),
        and the smallest value is used when a node has several rows, e.g. one
        per MPI rank.
        """
        nid_col = self.json_cols.index(self.nid_col_name)
        order_col = self.json_cols.index("Node order")

        node_order = {}
        for row in self.json_data:
            nid, slot = row[nid_col], row[order_col]
            if nid is None or slot is None:
                continue
            nid, slot = int(nid), int(slot)
            if nid not in node_order or slot < node_order[nid]:
                node_order[nid] = slot

        if not node_order:
            return node_order

        # Nodes without rows of their own (e.g. intermediate call-path frames)
        # inherit the smallest order in their subtree. Parents always precede
        # their children in the json-split "nodes" section.
        subtree_min = dict(node_order)
        for idx in range(len(self.json_nodes) - 1, -1, -1):
            parent = self.json_nodes[idx].get("parent")
            if idx in subtree_min and parent is not None:
                if parent not in subtree_min or subtree_min[idx] < subtree_min[parent]:
                    subtree_min[parent] = subtree_min[idx]
        for idx, slot in subtree_min.items():
            node_order.setdefault(idx, slot)

        return node_order

    def create_graph(self):
        list_roots = []

        global unknown_label_counter

        # find nodes in the nodes section that represent the path hierarchy
        for idx, node in enumerate(self.json_nodes):
            node_label = node["label"]
            if node_label == "":
                node_label = "UNKNOWN " + str(unknown_label_counter)
                unknown_label_counter += 1
            self.idx_to_label[idx] = node_label

            if node["column"] == self.path_col_name:
                # with node ordering, Caliper's call order becomes the
                # _hatchet_nid (self.node_order is empty otherwise)
                order = self.node_order.get(idx, -1)

                if "parent" not in node:
                    # since this node does not have a parent, this is a root
                    graph_root = Node(
                        Frame({"type": self.node_type, "name": node_label}), hnid=order
                    )
                    list_roots.append(graph_root)

                    node_dict = {
                        self.nid_col_name: idx,
                        "name": node_label,
                        "node": graph_root,
                    }
                    self.idx_to_node[idx] = node_dict
                else:
                    parent_hnode = (self.idx_to_node[node["parent"]])["node"]
                    hnode = Node(
                        Frame({"type": self.node_type, "name": node_label}),
                        hnid=order,
                    )
                    parent_hnode.add_child(hnode)

                    node_dict = {
                        self.nid_col_name: idx,
                        "name": node_label,
                        "node": hnode,
                    }
                    self.idx_to_node[idx] = node_dict

        return list_roots

    def read(self):
        """Read the caliper JSON file to extract the calling context tree."""
        with self.timer.phase("read json"):
            self.read_json_sections()

        with self.timer.phase("graph construction"):
            list_roots = self.create_graph()

        # create a dataframe of metrics from the data section
        self.df_json_data = pd.DataFrame(self.json_data, columns=self.json_cols)

        # when an nid has multiple entries due to the secondary hierarchy
        # we need to aggregate them for each (nid, rank)
        groupby_cols = [self.nid_col_name]
        if "rank" in self.json_cols:
            groupby_cols.append("rank")
        if "sourceloc#cali.sampler.pc" in self.json_cols:
            groupby_cols.append("sourceloc#cali.sampler.pc")

        if self.both_hierarchies is True:
            # create dict that stores aggregation function for each column
            agg_dict = {}
            for idx, item in enumerate(self.json_cols_mdata):
                col = self.json_cols[idx]
                if col != "rank" and col != "nid":
                    if item["is_value"] is True:
                        agg_dict[col] = np.sum
                    else:
                        agg_dict[col] = lambda x: x.iloc[0]

            grouped = self.df_json_data.groupby(groupby_cols).aggregate(agg_dict)
            self.df_json_data = grouped.reset_index()

        # map non-numeric columns to their mappings in the nodes section
        for idx, item in enumerate(self.json_cols_mdata):
            if item["is_value"] is False and self.json_cols[idx] != self.nid_col_name:
                if self.json_cols[idx] == "sourceloc#cali.sampler.pc":
                    # split source file and line number into two columns
                    self.df_json_data["file"] = self.df_json_data[
                        self.json_cols[idx]
                    ].apply(
                        lambda x: re.match(
                            r"(.*):(\d+)", self.json_nodes[x]["label"]
                        ).group(1)
                    )
                    self.df_json_data["line"] = self.df_json_data[
                        self.json_cols[idx]
                    ].apply(
                        lambda x: re.match(
                            r"(.*):(\d+)", self.json_nodes[x]["label"]
                        ).group(2)
                    )
                    self.df_json_data.drop(self.json_cols[idx], axis=1, inplace=True)
                    sourceloc_idx = idx
                elif self.json_cols[idx] == "path":
                    # we will only reach here if path is the "secondary"
                    # hierarchy in the data
                    self.df_json_data["path"] = self.df_json_data["path"].apply(
                        lambda x: (
                            None
                            if (math.isnan(x))
                            else self.json_nodes[int(x)]["label"]
                        )
                    )
                else:
                    self.df_json_data[self.json_cols[idx]] = self.df_json_data[
                        self.json_cols[idx]
                    ].apply(lambda x: self.json_nodes[x]["label"])

        # since we split sourceloc, we should update json_cols and
        # json_cols_mdata
        if "sourceloc#cali.sampler.pc" in self.json_cols:
            self.json_cols.pop(sourceloc_idx)
            self.json_cols_mdata.pop(sourceloc_idx)
            self.json_cols.append("file")
            self.json_cols.append("line")
            self.json_cols_mdata.append({"is_value": False})
            self.json_cols_mdata.append({"is_value": False})

        max_nid = self.df_json_data[self.nid_col_name].max()

        if "line" in self.df_json_data.columns:
            # split nodes that have multiple file:line numbers to have a child
            # each with a unique file:line number
            unique_nodes = self.df_json_data.groupby(self.nid_col_name)
            df_concat = [self.df_json_data]

            for nid, super_node in unique_nodes:
                line_groups = super_node.groupby("line")
                # only need to do something if there are more than one
                # file:line number entries for the node
                if len(line_groups.size()) > 1:
                    sn_hnode = self.idx_to_node[nid]["node"]

                    for line, line_group in line_groups:
                        # create the node label
                        file_path = (line_group.head(1))["file"].item()
                        file_name = os.path.basename(file_path)
                        node_label = file_name + ":" + line

                        # create a new hatchet node
                        max_nid += 1
                        idx = max_nid
                        hnode = Node(
                            Frame(
                                {"type": "statement", "file": file_path, "line": line}
                            ),
                            sn_hnode,
                        )
                        sn_hnode.add_child(hnode)

                        node_dict = {
                            self.nid_col_name: idx,
                            "name": node_label,
                            "node": hnode,
                        }
                        self.idx_to_node[idx] = node_dict

                        # change nid of the original node to new node in place
                        for index, row in line_group.iterrows():
                            self.df_json_data.loc[index, "nid"] = max_nid

                    # add new row for original node
                    node_copy = super_node.head(1).copy()
                    for cols in self.metric_columns:
                        node_copy[cols] = 0
                    df_concat.append(node_copy)

            # concatenate all the newly created dataframes with
            # self.df_json_data
            self.df_fixed_data = pd.concat(df_concat)
        else:
            self.df_fixed_data = self.df_json_data

        # create a dataframe with all nodes in the call graph
        self.df_nodes = pd.DataFrame.from_dict(data=list(self.idx_to_node.values()))

        # add missing intermediate nodes to the df_fixed_data dataframe
        if "rank" in self.json_cols:
            self.num_ranks = self.df_fixed_data["rank"].max() + 1
            rank_list = range(0, self.num_ranks)

        # create a standard dict to be used for filling all missing rows
        default_metric_dict = {}
        for idx, item in enumerate(self.json_cols_mdata):
            if self.json_cols[idx] != self.nid_col_name:
                if item["is_value"] is True:
                    default_metric_dict[self.json_cols[idx]] = 0
                else:
                    default_metric_dict[self.json_cols[idx]] = None

        # create a list of dicts, one dict for each missing row
        missing_nodes = []
        for iteridx, row in self.df_nodes.iterrows():
            # check if df_nodes row exists in df_fixed_data
            metric_rows = self.df_fixed_data.loc[
                self.df_fixed_data[self.nid_col_name] == row[self.nid_col_name]
            ]
            if "rank" not in self.json_cols:
                if metric_rows.empty:
                    # add a single row
                    node_dict = dict(default_metric_dict)
                    node_dict[self.nid_col_name] = row[self.nid_col_name]
                    missing_nodes.append(node_dict)
            else:
                if metric_rows.empty:
                    # add a row per MPI rank
                    for rank in rank_list:
                        node_dict = dict(default_metric_dict)
                        node_dict[self.nid_col_name] = row[self.nid_col_name]
                        node_dict["rank"] = rank
                        missing_nodes.append(node_dict)
                elif len(metric_rows) < self.num_ranks:
                    # add a row for each missing MPI rank
                    present_ranks = metric_rows["rank"].values
                    missing_ranks = [x for x in rank_list if x not in present_ranks]
                    for rank in missing_ranks:
                        node_dict = dict(default_metric_dict)
                        node_dict[self.nid_col_name] = row[self.nid_col_name]
                        node_dict["rank"] = rank
                        missing_nodes.append(node_dict)

        self.df_missing = pd.DataFrame.from_dict(data=missing_nodes)
        self.df_metrics = pd.concat([self.df_fixed_data, self.df_missing])

        # create a graph object once all the nodes have been added
        graph = Graph(list_roots, node_ordering=self.node_ordering)
        # we do not want to expose the "Node order" column to the user
        self.df_metrics = self.df_metrics.drop(columns="Node order", errors="ignore")
        graph.enumerate_traverse()

        # merge the metrics and node dataframes on the idx column
        with self.timer.phase("data frame"):
            dataframe = pd.merge(self.df_metrics, self.df_nodes, on=self.nid_col_name)
            # set the index to be a MultiIndex
            indices = ["node"]
            if "rank" in self.json_cols:
                indices.append("rank")
            dataframe.set_index(indices, inplace=True)
            dataframe.sort_index(inplace=True)

        # create list of exclusive and inclusive metric columns
        exc_metrics = []
        inc_metrics = []
        for column in self.metric_columns:
            if "(inc)" in column:
                inc_metrics.append(column)
            else:
                exc_metrics.append(column)

        return hatchet.graphframe.GraphFrame(
            graph, dataframe, exc_metrics, inc_metrics, metadata=self.metadata
        )
