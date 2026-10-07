#!/usr/bin/env python
#
# Copyright 2017-2023 Lawrence Livermore National Security, LLC and other
# Hatchet Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: MIT

import hatchet as ht


if __name__ == "__main__":
    # Path to caliper cali file.
    cali_file = (
        "../../../hatchet/tests/data/caliper-lulesh-cali/lulesh-annotation-profile.cali"
    )

    # This query supports raw .cali files from Caliper's aggregate and recorder
    # services. Use ``from_caliperreader`` for output from built-in configs such
    # as hatchet-region-profile. With ``cali-query`` before 2.10, use
    # ``prop:nested`` instead of ``path``.
    query = (
        "let sum#time.duration=scale(sum#time.duration.ns,1e-9) "
        "select path,"
        "sum(sum#time.duration) as time,"
        'inclusive_sum(sum#time.duration) as "time (inc)",'
        'min(aggregate.slot) as "Node order" '
        "group by path format json-split"
    )

    # Use hatchet's ``from_caliper`` API with the path to the cali file and the
    # query. This API will internally run ``cali-query`` on this file to
    # produce a json-split stream. The result is stored into Hatchet's
    # GraphFrame.
    gf = ht.GraphFrame.from_caliper(cali_file, query)

    # Printout the DataFrame component of the GraphFrame.
    print(gf.dataframe)

    # Printout the graph component of the GraphFrame.
    # Use "time (inc)" as the metric column to be displayed
    print(gf.tree(metric_column="time (inc)"))
