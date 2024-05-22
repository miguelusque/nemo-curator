# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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

import argparse
import time

import dask
from dask import dataframe as dd

from nemo_curator import FuzzyDuplicates, FuzzyDuplicatesConfig
from nemo_curator.datasets import DocumentDataset
from nemo_curator.utils.distributed_utils import get_client, write_to_disk
from nemo_curator.utils.script_utils import add_distributed_args


def pre_imports():
    import cudf  # noqa: F401


def main(args):

    dataset_dir = "/path/to/dataset"
    log_dir = "./"
    cache_dir = "./fuzzy_cache"
    output_dir = "./output"
    dataset_id_field = "id"
    dataset_text_field = "text"

    filetype = "parquet"

    # Fuzzy dup calculation only supports the cuDF/GPU backend
    backend = "cudf"
    assert args.device == "gpu"

    with dask.config.set({"dataframe.backend": backend}):
        client = get_client(args, args.device)
        client.run(pre_imports)

        t0 = time.time()
        if filetype == "parquet":
            input_dataset = DocumentDataset(
                dd.read_parquet(
                    dataset_dir,
                    columns=[dataset_id_field, dataset_text_field],
                    blocksize="256MiB",
                    aggregate_files=True,
                )
            )
        elif filetype == "jsonl":
            input_dataset = DocumentDataset.read_json(
                dataset_dir, backend=backend, input_meta=args.input_meta
            )

        fuzzy_dedup_config = FuzzyDuplicatesConfig(
            cache_dir=cache_dir,
            id_field=dataset_id_field,
            text_field=dataset_text_field,
            seed=42,
            char_ngrams=5,
            num_buckets=20,
            hashes_per_bucket=13,
            use_64_bit_hash=False,
            buckets_per_shuffle=5,
            false_positive_check=True,
            num_anchors=2,
            jaccard_threshold=0.8,
        )
        fuzzy_dup = FuzzyDuplicates(logger=log_dir, config=fuzzy_dedup_config)
        duplicates = fuzzy_dup(dataset=input_dataset)

        # By default all duplicate id's and the group they belong to are included in the result
        # keep 1 document from each group of duplcates and mark the others to remove
        # https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.duplicated.html
        docs_to_remove = duplicates.df.map_partitions(
            lambda x: x[x.group.duplicated(keep="first")]
        )

        # When there are few duplicates we can compute the results to a list and use `isin`.
        result = input_dataset.df[
            ~input_dataset.df[dataset_id_field].isin(
                docs_to_remove[dataset_id_field].compute()
            )
        ]
        write_to_disk(result, output_dir, output_type=filetype)
        print(time.time() - t0)


def attach_args(
    parser=argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    ),
):
    parser.add_argument(
        "--input-meta",
        type=str,
        default=None,
        help="A dictionary containing the json object field names and their "
        "corresponding data types.",
    )

    return add_distributed_args(parser)


if __name__ == "__main__":
    main(attach_args().parse_args())
