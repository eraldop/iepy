# -*- coding: utf-8 -*-
"""
IEPY's main module. Implements a bootstrapped information extraction
pipeline conceptually similar to SNOWBALL [snowball]_ or DIPRE [dipre]_.

This kind of pipeline is traditionally composed of 6 stages:

    1. Use seed fact to gather text that evidences the facts
    2. Filter text evidence
    3. Learn extraction patterns using evidence
    4. Filter extraction patterns
    5. Use extraction patterns to generate facts from corpus
    6. Filter generated facts

And these stages are iterated by adding the filtered facts resulting from
(6) into the seed facts used by (1), thus making the "boostrap" part of the
boostrapped pipeline.

In this particular instantiation of that pipeline the stages are
implemented as follows:

    1. Seed facts are given at initialization and text comes from a
       database previously constructed. Evidence is over-generated by
       returning any text segment that contains entities of matching type
       (ex. person-person, person-place, etc.).
    2. Evidence is filtered by a human using this class' API. When the
       human gets tired of answering queries it jumps to the next pipeline
       stage.
    3. A statistical classifier is learnt for every relation so that the
       classifier is able to tell if a given text segment contains or not
       the manifestation of a fact.
    4. No filtering of the classifiers is made, so this stage is a no-op.
    5. Every text segment is passed through every classifier to determine
       if a fact is present or not. All classifications are returned
       along with a score between 0 and 1 indicating the probability that
       a fact is present in that text segment.
    6. Facts are filtered with a threshold on the probability of the
       classification. This threshold is a class atribute meant to be tuned
       by the Iepy user.

.. [snowball] Snowball: Extracting Relations from Large Plain-Text Collections.
           Agichtein & Gravano 1999

.. [dipre] Extracting Patterns and Relations from the World Wide Web.
        Brin 1999
"""

from copy import deepcopy
import itertools
import logging

from iepy import db
from iepy.fact_extractor import FactExtractorFactory
from iepy.utils import evaluate

from iepy import defaults
from iepy.knowledge import certainty, Evidence, Fact, Knowledge

logger = logging.getLogger(__name__)


class BootstrappedIEPipeline(object):
    """
    Iepy's main class. Implements a boostrapped information extraction pipeline.

    From the user's point of view this class is meant to be used like this::

        p = BoostrappedIEPipeline(db_connector, seed_facts)
        p.start()  # blocking
        while UserIsNotTired:
            for question in p.questions_available():
                # Ask user
                # ...
                p.add_answer(question, answer)
            p.force_process()
        facts = p.get_facts()  # profit
    """

    def __init__(self, db_connector, seed_facts, gold_standard=None,
                 extractor_config=None, prediction_config=None,
                 evidence_threshold=defaults.evidence_threshold,
                 fact_threshold=defaults.fact_threshold,
                 sort_questions_by=defaults.questions_sorting,
                 drop_guesses_each_round=False):
        """
        Not blocking.
        """
        self.db_con = db_connector
        self.knowledge = Knowledge({Evidence(f, None, None, None): 1 for f in seed_facts})
        self.evidence_threshold = evidence_threshold
        self.fact_threshold = fact_threshold
        self.questions = Knowledge()
        self.answers = {}
        self.gold_standard = gold_standard
        self.extractor_config = deepcopy(extractor_config or defaults.extractor_config)
        self.prediction_config = deepcopy(prediction_config or defaults.prediction_config)
        self.sort_questions_by = sort_questions_by
        self.drop_guesses_each_round = drop_guesses_each_round

        self.steps = [
                self.generalize_knowledge,   # Step 1
                self.generate_questions,     # Step 2, first half
                None,                        # Pause to wait question answers
                self.filter_evidence,        # Step 2, second half
                self.learn_fact_extractors,  # Step 3
                self.extract_facts,          # Step 5
                self.filter_facts,           # Step 6
                self.evaluate                # Optional evaluation step
        ]
        self.step_iterator = itertools.cycle(self.steps)

        # Build relation description: a map from relation labels to pairs of entity kinds
        self.relations = {}
        for e in self.knowledge:
            t1 = e.fact.e1.kind
            t2 = e.fact.e2.kind
            if e.fact.relation in self.relations and (t1, t2) != self.relations[e.fact.relation]:
                raise ValueError("Ambiguous kinds for relation %r" % e.fact.relation)
            self.relations[e.fact.relation] = (t1, t2)
        # Precompute all the evidence that must be classified
        self.evidence = evidence = Knowledge()
        for r, (lkind, rkind) in self.relations.items():
            for segment in self.db_con.segments.segments_with_both_kinds(lkind, rkind):
                for o1, o2 in segment.kind_occurrence_pairs(lkind, rkind):
                    e1 = db.get_entity(segment.entities[o1].kind, segment.entities[o1].key)
                    e2 = db.get_entity(segment.entities[o2].kind, segment.entities[o2].key)
                    f = Fact(e1, r, e2)
                    e = Evidence(f, segment, o1, o2)
                    evidence[e] = 0.5

    def do_iteration(self, data):
        for step in self.step_iterator:
            if step is None:
                return
            data = step(data)

    ###
    ### IEPY User API
    ###

    def start(self):
        """
        Blocking.
        """
        logger.info(u'Starting pipeline with {} seed '
                    u'facts'.format(len(self.knowledge)))
        self.do_iteration(self.knowledge)

    def questions_available(self):
        """
        Not blocking.
        Returned value won't change until a call to `add_answer` or
        `force_process`.
        If `id` of the returned value hasn't changed the returned value is the
        same.
        The available questions are a list of evidence.
        """
        if self.sort_questions_by == 'score':
            return self.questions.by_score(reverse=True)
        else:
            assert self.sort_questions_by == 'certainty'
            # TODO: Check: latest changes on generate_questions probably demand
            # some extra work on the following line to have back the usual
            # sort by certainty
            return self.questions.by_certainty()

    def add_answer(self, evidence, answer):
        """
        Blocking (potentially).
        After calling this method the values returned by `questions_available`
        and `known_facts` might change.
        """
        self.answers[evidence] = int(answer)

    def force_process(self):
        """
        Blocking.
        After calling this method the values returned by `questions_available`
        and `known_facts` might change.
        """
        self.do_iteration(None)

    def known_facts(self):
        """
        Not blocking.
        Returned value won't change until a call to `add_answer` or
        `force_process`.
        If `len` of the returned value hasn't changed the returned value is the
        same.
        """
        return self.knowledge

    ###
    ### Pipeline steps
    ###

    def generalize_knowledge(self, knowledge):
        """
        Stage 1 of pipeline.

        Based on the known facts (knowledge), generates all possible
        evidences of them. The generated evidence is scored using the scores
        given to the facts.
        """
        logger.debug(u'running generalize_knowledge')
        # XXX: there may be several scores for the same fact in knowledge.
        fact_knowledge = dict((e.fact, s) for e, s in knowledge.items())
        knowledge_evidence = Knowledge((e, fact_knowledge[e.fact])
                    for e, _ in self.evidence.items() if e.fact in fact_knowledge)
        logger.info(u'Found {} potential evidences where the known facts could'
                    u' manifest'.format(len(knowledge_evidence)))
        return knowledge_evidence

    def generate_questions(self, knowledge_evidence):
        """
        Stage 2.1 of pipeline.

        Stores unanswered questions in self.questions and stops. Questions come
        from generalized evidence for known facts (knowledge_evidence), with
        high scores, and from undecided evidence scored by the last classifier
        in step 5 (self.evidence).
        """
        logger.debug(u'running generate_questions')
        # first add all evidence, then override scores for fact_evidence.
        self.questions = Knowledge((e, s) for e, s in self.evidence.items()
                                                    if e not in self.answers)
        self.questions.update((e, s) for e, s in knowledge_evidence.items()
                                                    if e not in self.answers)

    def filter_evidence(self, _):
        """
        Stage 2.2 of pipeline.

        Build evidence for training the classifiers, from user answers
        (self.answers) and unanswered evidence (self.evidence) with last
        classification score certainty over self.evidence_threshold.
        """
        logger.debug(u'running filter_evidence')
        evidence = Knowledge(self.answers)
        n = len(evidence)
        evidence.update(
            (e, score > 0.5 and 1 or 0)
            for e, score in self.evidence.items()
            if certainty(score) > self.evidence_threshold and e not in self.answers
        )
        logger.info(u'Filtering returns {} human-built evidences and {} '
                    u'over-threshold evidences'.format(n, len(evidence) - n))
        return evidence

    def learn_fact_extractors(self, evidence):
        """
        Stage 3 of pipeline.
        evidence is a Knowledge instance of {evidence: is_good_evidence}
        """
        logger.debug(u'running learn_fact_extractors')
        classifiers = {}
        for rel, k in evidence.per_relation().items():
            yesno = set(k.values())
            if True not in yesno or False not in yesno:
                logger.warning(u'Not enough evidence to train a fact extractor'
                               u' for the "{}" relation'.format(rel))
                continue  # Not enough data to train a classifier
            assert len(yesno) == 2, "Evidence is not binary!"
            logger.info(u'Training "{}" relation with {} '
                        u'evidences'.format(rel, len(k)))
            classifiers[rel] = self._build_extractor(rel, Knowledge(k))
        return classifiers

    def _build_extractor(self, relation, data):
        """Actual invocation of classifier"""
        if self.extractor_config['classifier'] == 'labelspreading':
            # semi-supervised learning: add unlabeled data
            data.update((e, -1) for e in self.evidence if e not in data)
        return FactExtractorFactory(self.extractor_config, data)

    def _score_evidence(self, relation, classifier, evidence_list):
        """Given a classifier and a list of evidences, predict if they
        are positive evidences or not.
        Depending on the settings, prediction can be:
            - probabilistic or binary
            - scaled to a range, or not
        """
        # TODO: Is probably cleaner if this logic is inside FactExtractorFactory
        if classifier:
            method = self.prediction_config['method']
            ps = getattr(classifier, method)(evidence_list)
            if self.prediction_config['scale_to_range']:
                # scale scores to a given range
                range_min, range_max = sorted(self.prediction_config['scale_to_range'])
                range_delta = range_max - range_min
                max_score = max(ps)
                min_score = min(ps)
                score_range = max_score - min_score
                scale = lambda x: (x - min_score) * range_delta / score_range + range_min
                ps = map(scale, ps)
        else:
            # There was no evidence to train this classifier
            ps = [0.5] * len(evidence_list)  # Maximum uncertainty
        logger.info(u'Estimated fact manifestation probabilities for {} '
                    u'potential evidences for "{}" '
                    u'relation'.format(len(ps), relation))
        return ps

    def extract_facts(self, classifiers):
        """
        Stage 5 of pipeline.
        classifiers is a dict {relation: classifier, ...}
        """
        # TODO: this probably is smarter as an outer iteration through segments
        # and then an inner iteration over relations
        logger.debug(u'running extract_facts')
        result = Knowledge()

        for r, evidence in self.evidence.per_relation().items():
            evidence = list(evidence)
            ps = self._score_evidence(r, classifiers.get(r, None), evidence)
            result.update(zip(evidence, ps))
        # save scores for later use (e.g. in generate_questions, stage 2.1)
        self.evidence.update(result)
        return result

    def filter_facts(self, facts):
        """
        Stage 6 of pipeline.
        facts is [((a, b, relation), confidence), ...]
        """
        logger.debug(u'running filter_facts')
        if self.drop_guesses_each_round:
            logger.info(u'Discarding previously auto-accepted evidence.')
            self.knowledge = Knowledge(
                (e, answer) for (e, answer) in self.answers.items() if answer
            )
        n = len(self.knowledge)
        self.knowledge.update((e, s) for e, s in facts.items()
                              if s > self.fact_threshold)
        logger.debug(u'  classifiers accepted {} new evidences'.format(len(self.knowledge) - n))
        # unlearn user negative answers:
        m = len(self.knowledge)
        for e, s in self.answers.items():
            if s == 0 and e in self.knowledge:
                del self.knowledge[e]
        logger.debug(u'  user answers removed {} evidences'.format(m - len(self.knowledge)))

        logger.info(u'Learnt {} new evidences this iteration (adding to a total '
                    u'of {} evidences)'.format(len(self.knowledge) - n,
                                               len(self.knowledge)))

        return self.knowledge

    def evaluate(self, knowledge):
        """
        If a gold standard was given, compute precision and recall for current
        knowledge.
        """
        if self.gold_standard:
            logger.debug(u'running evaluate')
            result = evaluate(knowledge, self.gold_standard)
            logger.info(u'Precision: {}'.format(result['precision']))
            logger.info(u'Recall: {}'.format(result['recall']))

        return knowledge

    ###
    ### Aux methods
    ###
    def _confidence(self, evidence):
        """
        Returns a probability estimation of segment being an manifestation of
        fact.
        fact is (a, b, relation).
        """
        if evidence in self.knowledge:
            return self.knowledge[evidence]

        # FIXME: to be implemented on ticket IEPY-47
        return 0.5
