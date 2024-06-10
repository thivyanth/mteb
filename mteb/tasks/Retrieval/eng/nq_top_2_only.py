from __future__ import annotations

from mteb.abstasks.TaskMetadata import TaskMetadata

from ....abstasks.AbsTaskRetrieval import AbsTaskRetrieval


class nq_top_2_only(AbsTaskRetrieval):
    metadata = TaskMetadata(
        name="nq_top_2_only",
        dataset={
			"path": "mteb/nq_top_2_only",
			"revision": "abd25eb168bb00a778548d2bdde66fbddeaeb438",
        },
        description="NFCorpus: A Full-Text Learning to Rank Dataset for Medical Information Retrieval",
        reference="https://ai.google.com/research/NaturalQuestions/",
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
