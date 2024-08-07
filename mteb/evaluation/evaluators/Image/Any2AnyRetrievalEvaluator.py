from __future__ import annotations

import heapq
import json
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Tuple

import numpy as np
import pytrec_eval
import torch
from datasets import Dataset
from PIL import Image
from torch.utils.data import DataLoader
from torchvision import transforms

from mteb.encoder_interface import EncoderWithQueryCorpusEncode

from ..Evaluator import Evaluator
from ..utils import (
    confidence_scores,
    cos_sim,
    dot_score,
    download,
    hole,
    mrr,
    nAUC,
    recall_cap,
    top_k_accuracy,
)

logger = logging.getLogger(__name__)

transform = transforms.Compose([transforms.PILToTensor()])


class ImageDataset(torch.utils.data.Dataset):
    def __init__(self, hf_dataset, image_column_name: str = "image", transform=None):
        self.dataset = hf_dataset
        self.transform = transform
        self.image_column_name = image_column_name

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx):
        image = self.dataset[idx][self.image_column_name]
        if image.mode != "RGB":
            image = image.convert("RGB")
        image = self.transform(image)
        return image


def custom_collate_fn(batch):
    return batch


# Adapted from https://github.com/beir-cellar/beir/blob/f062f038c4bfd19a8ca942a9910b1e0d218759d4/beir/retrieval/search/dense/exact_search.py#L12
class DenseRetrievalExactSearch:
    def __init__(
        self,
        model: EncoderWithQueryCorpusEncode,
        encode_kwargs: dict[str, Any] = {},
        corpus_chunk_size: int = 20000,
        previous_results: str | None = None,
        **kwargs: Any,
    ):
        # Model is class that provides get_text_embeddings() and get_image_embeddings()
        self.model = model
        self.encode_kwargs = encode_kwargs

        if "batch_size" not in encode_kwargs:
            encode_kwargs["batch_size"] = 128

        self.score_functions = {"cos_sim": cos_sim, "dot": dot_score}
        self.score_function_desc = {
            "cos_sim": "Cosine Similarity",
            "dot": "Dot Product",
        }
        self.corpus_chunk_size = corpus_chunk_size
        self.previous_results = previous_results
        self.batch_size = encode_kwargs.get("batch_size")
        self.show_progress_bar = encode_kwargs.get("show_progress_bar")
        self.save_corpus_embeddings = kwargs.get("save_corpus_embeddings", False)
        self.corpus_embeddings = defaultdict(list)
        self.results = {}

        if self.previous_results is not None:
            self.previous_results = self.load_results_file()

    def search(
        self,
        corpus: Dataset,  # solve memoery issues
        queries: Dataset,  # solve memoery issues
        top_k: int,
        score_function: str,
        return_sorted: bool = False,
        **kwargs,
    ) -> dict[str, dict[str, float]]:
        if score_function not in self.score_functions:
            raise ValueError(
                f"score function: {score_function} must be either (cos_sim) for cosine similarity or (dot) for dot product"
            )

        logger.info("Encoding Queries.")
        query_ids = list(queries["id"])
        self.results = {qid: {} for qid in query_ids}

        q_modality = queries[0]["modality"]

        if q_modality == "text":
            query_texts = queries["text"]
            query_embeddings = self.model.get_text_embeddings(
                texts=query_texts, batch_size=self.encode_kwargs["batch_size"]
            )
        else:
            queries_dataset = ImageDataset(
                queries, image_column_name="image", transform=transform
            )
            query_image_dataloader = DataLoader(
                queries_dataset,
                batch_size=self.encode_kwargs["batch_size"],
                shuffle=False,
                collate_fn=custom_collate_fn,
                num_workers=os.cpu_count(),
            )
            if q_modality == "image":
                query_embeddings = self.model.get_image_embeddings(
                    images=query_image_dataloader,
                    batch_size=self.encode_kwargs["batch_size"],
                )
            elif q_modality == "image,text":
                query_texts = queries["text"]
                query_embeddings = self.model.get_fused_embeddings(
                    texts=query_texts,
                    images=query_image_dataloader,
                    batch_size=self.encode_kwargs["batch_size"],
                )
            else:
                raise ValueError(f"Unsupported modality: {q_modality}")

        logger.info("Preparing Corpus...")
        corpus_ids = list(corpus["id"])

        corpus_modality = corpus[0]["modality"]

        logger.info("Encoding Corpus in batches... Warning: This might take a while!")
        logger.info(
            "Scoring Function: {} ({})".format(
                self.score_function_desc[score_function], score_function
            )
        )

        result_heaps = {qid: [] for qid in query_ids}
        for chunk_start in range(0, len(corpus), self.corpus_chunk_size):
            chunk = corpus.select(
                range(
                    chunk_start, min(chunk_start + self.corpus_chunk_size, len(corpus))
                )
            )
            chunk_ids = corpus_ids[chunk_start : chunk_start + self.corpus_chunk_size]

            if corpus_modality == "text":
                corpus_texts = chunk["text"]
                sub_corpus_embeddings = self.model.get_text_embeddings(
                    texts=corpus_texts, batch_size=self.encode_kwargs["batch_size"]
                )
            else:
                corpus_dataset = ImageDataset(
                    chunk, image_column_name="image", transform=transform
                )
                corpus_image_dataloader = DataLoader(
                    corpus_dataset,
                    batch_size=self.encode_kwargs["batch_size"],
                    shuffle=False,
                    collate_fn=custom_collate_fn,
                    num_workers=os.cpu_count(),
                )
                if corpus_modality == "image":
                    sub_corpus_embeddings = self.model.get_image_embeddings(
                        images=corpus_image_dataloader,
                        batch_size=self.encode_kwargs["batch_size"],
                    )
                elif corpus_modality == "image,text":
                    corpus_texts = chunk["text"]
                    sub_corpus_embeddings = self.model.get_fused_embeddings(
                        texts=corpus_texts,
                        images=corpus_image_dataloader,
                        batch_size=self.encode_kwargs["batch_size"],
                    )
                else:
                    raise ValueError(f"Unsupported modality: {corpus_modality}")

            cos_scores = self.score_functions[score_function](
                query_embeddings, sub_corpus_embeddings
            )
            cos_scores[torch.isnan(cos_scores)] = -1

            cos_scores_top_k_values, cos_scores_top_k_idx = torch.topk(
                cos_scores,
                top_k,
                dim=1,
                largest=True,
                sorted=return_sorted,
            )
            cos_scores_top_k_values = cos_scores_top_k_values.cpu().tolist()
            cos_scores_top_k_idx = cos_scores_top_k_idx.cpu().tolist()

            for query_itr in range(len(query_embeddings)):
                query_id = query_ids[query_itr]
                for sub_corpus_id, score in zip(
                    cos_scores_top_k_idx[query_itr], cos_scores_top_k_values[query_itr]
                ):
                    corpus_id = chunk_ids[sub_corpus_id]
                    if len(result_heaps[query_id]) < top_k:
                        heapq.heappush(result_heaps[query_id], (score, corpus_id))
                    else:
                        heapq.heappushpop(result_heaps[query_id], (score, corpus_id))

        for qid in result_heaps:
            for score, corpus_id in result_heaps[qid]:
                self.results[qid][corpus_id] = score

        return self.results

    def load_results_file(self):
        # load the first stage results from file in format {qid: {doc_id: score}}
        if "https://" in self.previous_results:
            # download the file
            if not os.path.exists(self.previous_results):
                url_descriptor = self.previous_results.split("https://")[-1].replace(
                    "/", "--"
                )
                dest_file = os.path.join(
                    "results", f"cached_predictions--{url_descriptor}"
                )
                os.makedirs(os.path.dirname(os.path.abspath(dest_file)), exist_ok=True)
                download(self.previous_results, dest_file)
                logger.info(
                    f"Downloaded the previous results at {self.previous_results} to {dest_file}"
                )
            self.previous_results = dest_file

        with open(self.previous_results, "r") as f:
            previous_results = json.load(f)
        assert isinstance(previous_results, dict)
        assert isinstance(previous_results[list(previous_results.keys())[0]], dict)
        return previous_results


# Adapted from https://github.com/beir-cellar/beir/blob/f062f038c4bfd19a8ca942a9910b1e0d218759d4/beir/retrieval/evaluation.py#L9
class Any2AnyRetrievalEvaluator(Evaluator):
    def __init__(
        self,
        retriever=None,
        task_name: str | None = None,
        k_values: List[int] = [1, 3, 5, 10, 20, 100, 1000],
        score_function: str = "cos_sim",
        encode_kwargs: dict[str, Any] = {},
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.retriever = DenseRetrievalExactSearch(
            retriever, encode_kwargs=encode_kwargs, **kwargs
        )
        self.k_values = k_values
        self.top_k = (
            max(k_values) if "top_k" not in kwargs else kwargs["top_k"]
        )  # can lower it if reranking
        self.score_function = score_function
        self.task_name = task_name

    def __call__(
        self,
        corpus: dict[str, Dict[str, str | Image.Image]],
        queries: dict[str, Dict[str, str | Image.Image]],
    ) -> dict[str, dict[str, float]]:
        if not self.retriever:
            raise ValueError("Model/Technique has not been provided!")

        return self.retriever.search(
            corpus,
            queries,
            self.top_k,
            self.score_function,
            prompt_name=self.task_name,  # type: ignore
        )

    @staticmethod
    def evaluate(
        qrels: dict[str, dict[str, int]],
        results: dict[str, dict[str, float]],
        k_values: List[int],
        ignore_identical_ids: bool = False,
    ) -> Tuple[
        dict[str, float],
        dict[str, float],
        dict[str, float],
        dict[str, float],
        dict[str, float],
    ]:
        if ignore_identical_ids:
            logger.debug(
                "For evaluation, ``ignore_identical_ids=True`` is set to True, the evaluator will ignore identical query and document ids."
            )
            # Remove identical ids from results dict
            for qid, rels in results.items():
                for pid in list(rels):
                    if qid == pid:
                        results[qid].pop(pid)
        else:
            logger.debug(
                "For evaluation, we DO NOT ignore identical query and document ids (default), please explicitly set ``ignore_identical_ids=True`` to ignore this."
            )

        all_ndcgs, all_aps, all_recalls, all_precisions = {}, {}, {}, {}

        for k in k_values:
            all_ndcgs[f"NDCG@{k}"] = []
            all_aps[f"MAP@{k}"] = []
            all_recalls[f"Recall@{k}"] = []
            all_precisions[f"P@{k}"] = []

        map_string = "map_cut." + ",".join([str(k) for k in k_values])
        ndcg_string = "ndcg_cut." + ",".join([str(k) for k in k_values])
        recall_string = "recall." + ",".join([str(k) for k in k_values])
        precision_string = "P." + ",".join([str(k) for k in k_values])
        evaluator = pytrec_eval.RelevanceEvaluator(
            qrels, {map_string, ndcg_string, recall_string, precision_string}
        )
        scores = evaluator.evaluate(results)

        for query_id in scores.keys():
            for k in k_values:
                all_ndcgs[f"NDCG@{k}"].append(scores[query_id]["ndcg_cut_" + str(k)])
                all_aps[f"MAP@{k}"].append(scores[query_id]["map_cut_" + str(k)])
                all_recalls[f"Recall@{k}"].append(scores[query_id]["recall_" + str(k)])
                all_precisions[f"P@{k}"].append(scores[query_id]["P_" + str(k)])

        ndcg, _map, recall, precision = (
            all_ndcgs.copy(),
            all_aps.copy(),
            all_recalls.copy(),
            all_precisions.copy(),
        )

        for k in k_values:
            ndcg[f"NDCG@{k}"] = round(sum(ndcg[f"NDCG@{k}"]) / len(scores), 5)
            _map[f"MAP@{k}"] = round(sum(_map[f"MAP@{k}"]) / len(scores), 5)
            recall[f"Recall@{k}"] = round(sum(recall[f"Recall@{k}"]) / len(scores), 5)
            precision[f"P@{k}"] = round(sum(precision[f"P@{k}"]) / len(scores), 5)

        naucs = Any2AnyRetrievalEvaluator.evaluate_abstention(
            results, {**all_ndcgs, **all_aps, **all_recalls, **all_precisions}
        )

        return ndcg, _map, recall, precision, naucs

    @staticmethod
    def evaluate_custom(
        qrels: dict[str, dict[str, int]],
        results: dict[str, dict[str, float]],
        k_values: List[int],
        metric: str,
        output_type: str = "all",
    ) -> Tuple[Dict[str, float]]:
        if metric.lower() in ["mrr", "mrr@k", "mrr_cut"]:
            metric_scores = mrr(qrels, results, k_values, output_type)

        elif metric.lower() in ["recall_cap", "r_cap", "r_cap@k"]:
            metric_scores = recall_cap(qrels, results, k_values, output_type)

        elif metric.lower() in ["hole", "hole@k"]:
            metric_scores = hole(qrels, results, k_values, output_type)

        elif metric.lower() in [
            "acc",
            "top_k_acc",
            "accuracy",
            "accuracy@k",
            "top_k_accuracy",
        ]:
            metric_scores = top_k_accuracy(qrels, results, k_values, output_type)

        naucs = Any2AnyRetrievalEvaluator.evaluate_abstention(results, metric_scores)
        metric_scores_avg = {k: sum(v) / len(v) for k, v in metric_scores.items()}

        return metric_scores_avg, naucs

    @staticmethod
    def evaluate_abstention(
        results: dict[str, dict[str, float]],
        metric_scores: dict[str, list[float]],
    ) -> Dict[str, float]:
        """Computes normalized Area Under the Curve on a set of evaluated instances as presented in the paper https://arxiv.org/abs/2402.12997"""
        all_sim_scores = [list(results[qid].values()) for qid in list(results.keys())]
        all_conf_scores = [
            confidence_scores(sim_scores) for sim_scores in all_sim_scores
        ]
        conf_fcts = list(all_conf_scores[0].keys())
        all_conf_scores = {
            fct: np.array([x[fct] for x in all_conf_scores]) for fct in conf_fcts
        }
        metric_scores = {k: np.array(v) for k, v in metric_scores.items()}
        naucs = {}

        for metric_name, scores in metric_scores.items():
            for fct, conf_scores in all_conf_scores.items():
                naucs[f"nAUC_{metric_name}_{fct}"] = nAUC(conf_scores, scores)

        return naucs
