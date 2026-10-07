#!/usr/bin/env python
#
# Copyright 2017-2023 Lawrence Livermore National Security, LLC and other
# Hatchet Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: MIT

import subprocess
import hatchet as ht


if __name__ == "__main__":
    # Path to caliper cali file.
    cali_file = (
        "../../../hatchet/tests/data/caliper-lulesh-cali/lulesh-annotation-profile.cali"
    )

    # Setup desired cali query. It works for files recorded with any Caliper
    # version; the "Read in a Caliper cali file" example (caliper_cali_query.py)
    # explains each clause.
    cali_query = "cali-query"
    query = (
        "let sum#time.duration=scale(sum#time.duration.ns,1e-9) "
        "select path,"
        "sum(sum#time.duration) as time,"
        'inclusive_sum(sum#time.duration) as "time (inc)",'
        'min(aggregate.slot) as "Node order" '
        "group by path format json-split"
    )

    # Use ``cali-query`` here to produce the json-split stream.
    cali_json = subprocess.Popen(
        [cali_query, "-q", query, cali_file], stdout=subprocess.PIPE
    )

    # Use hatchet's ``from_caliper`` API with the resulting json-split.
    # The result is stored into Hatchet's GraphFrame.
    gf = ht.GraphFrame.from_caliper(cali_json.stdout)

    # Printout the DataFrame component of the GraphFrame.
    print(gf.dataframe)

    # Printout the graph component of the GraphFrame.
    # Use "time (inc)" as the metric column to be displayed
    print(gf.tree(metric_column="time (inc)"))
