from __future__ import annotations

from mteb.abstasks.TaskMetadata import TaskMetadata

from ....abstasks.AbsTaskRetrieval import AbsTaskRetrieval


class trec_covid_top_2_only_w_correct(AbsTaskRetrieval):
    metadata = TaskMetadata(
        name="trec_covid_top_2_only_w_correct",
        description="trec_covid_top_2_only_w_correct is an ad-hoc search challenge based on the COVID-19 dataset containing scientific articles related to the COVID-19 pandemic.",
        reference="https://ir.nist.gov/covidSubmit/index.html",
        dataset={
			"path": "mteb/trec_covid_top_2_only_w_correct",
			"revision": "f1770cd66f9f496c197c9b5292561fd14b1260b0",
        },
        type="Retrieval",
        category="s2p",
        eval_splits=["test"],
        eval_langs=["eng-Latn"],
        main_score="ndcg_at_10",
        date=None,
        form=None,
        domains=None,
        task_subtypes=None,
        license=None,
        socioeconomic_status=None,
        annotations_creators=None,
        dialect=None,
        text_creation=None,
        bibtex_citation=None,
        n_samples=None,
        avg_character_length=None,
    )
