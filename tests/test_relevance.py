from copy import deepcopy
import json
import sqlite3
import unittest

from indeces.memory import MemoryGraph
from indeces.relevance import contains, metadata_factor, normalize, plan, source_hint
from indeces.run_records import digest, freeze_retrieval, validate_graph_audit


class ConceptRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.graph = MemoryGraph(self.db, retrieval_policy='concept_v1')

    def tearDown(self):
        self.db.close()

    def add(self, text, marks, source='paper', scope='scope'):
        return self.graph.add(scope, source, 'author',
                              [{'text': text, 'quote': text, 'marks': marks}], 1)[0]['id']

    def retrieve(self, query, **kwargs):
        audit = {}
        result = self.graph.retrieve('scope', [], query, 2, audit=audit, **kwargs)
        validate_graph_audit(audit)
        frozen = freeze_retrieval(self.db, 'scope', audit['event_id'], query, result, audit)
        validate_graph_audit(frozen['graph_audit'], frozen)
        return result, audit

    def test_normalizes_layout_and_case_without_erasing_charge_or_star(self):
        self.assertTrue(contains('electronic temperature', 'Electronic-Temperature'))
        self.assertTrue(contains('descriptor', 'descrip-\ntor'))
        self.assertTrue(contains('cv peak', 'CV   PEAK'))
        self.assertFalse(contains('ho*', 'hoo*'))
        self.assertFalse(contains('cat', 'catalyst'))
        self.assertNotEqual(normalize('OH−'), normalize('OH'))

    def test_short_concepts_survive_many_long_bibliographic_hits(self):
        marks = ['long publication subject ' + str(i) for i in range(12)]
        for i, mark in enumerate(marks):
            self.add(mark, [mark], source='title-' + str(i))
        answer = self.add('The cv peak is reversible at the reference electrode.', ['cv peak', 'rhe'])
        query = 'In “' + '; '.join(marks) + '” (2025), what CV peak versus RHE is reversible?'
        selected, audit = self.retrieve(query)
        self.assertIn('cv peak', audit['match']['direct_hits'])
        self.assertIn('rhe', audit['match']['direct_hits'])
        self.assertIn(answer, [r['id'] for r in selected])

    def test_body_index_covers_a_concept_missing_from_all_labels(self):
        answer = self.add('The electronic-temperature for the experiment is explicitly stated here.', ['instrument'])
        result, audit = self.retrieve('What electronic temperature is stated?')
        self.assertEqual(result[0]['id'], answer)
        self.assertEqual(result[0]['direct_marks'], [])
        self.assertIn(answer, audit['selection']['concept_policy']['lexical_ids'])

    def test_references_lose_density_advantage_but_titles_remain_eligible(self):
        body = self.add('The isotope ratio is measured independently.', ['isotope ratio'])
        bibliography = self.add('1. A. Smith, Isotope ratio mass spectra, 2015.\n2. B. Jones, Isotope ratio mass spectra, 2016.',
                                ['isotope ratio', 'mass spectra', 'spectra', 'mass'], source='refs')
        title = self.add('Isotope ratio instrument specification', ['instrument specification'], source='title')
        result, audit = self.retrieve('What isotope ratio is measured by mass spectra?')
        ranks = {c['record_id']: c['rank'] for c in audit['selection']['ranked_candidates']}
        self.assertLess(ranks[body], ranks[bibliography])
        title_result, _ = self.retrieve('instrument specification')
        self.assertEqual(title_result[0]['id'], title)

    def test_numbered_methods_are_not_classified_as_references(self):
        text = '5. At another pH, correct the entropy.\n6. We calculate energies.'
        self.assertEqual(metadata_factor(text), ('body_or_title', 1.0))

    def test_generic_list_question_rewards_enumeration(self):
        answer = self.add('We include alpha, beta, gamma, and delta classes. Their calibration is universal.', ['materials'])
        self.add('Calibration is used in many settings and publications.', ['calibration'], source='other')
        result, _ = self.retrieve('In which classes is calibration validated?')
        self.assertEqual(result[0]['id'], answer)

    def test_context_identity_inherits_only_for_anaphoric_queries(self):
        previous = 'In “A long publication title for a reference study” (2023, DOI 10.1234/example), what parameter is used?'
        inherited = plan('在这套分析中具体是什么descriptor?', previous)
        self.assertEqual(inherited['dois'], ['10.1234/example'])
        self.assertEqual(plan('What is the parameter in another experiment?', previous)['dois'], [])
        explicit = plan('In this study DOI 10.9999/new, what parameter is used?', previous)
        self.assertEqual(explicit['dois'], ['10.9999/new'])

    def test_archival_and_scope_gates_apply_to_body_index(self):
        old = self.add('The cobalt test is obsolete.', [], source='old')
        other = self.add('The cobalt test is private elsewhere.', [], source='other', scope='other')
        active = self.add('The cobalt test is current.', [], source='current')
        self.graph.deactivate_source('scope', 'old')
        result, audit = self.retrieve('cobalt test')
        self.assertEqual([r['id'] for r in result], [active])
        self.assertNotIn(old, audit['selection']['concept_policy']['lexical_ids'])
        self.assertNotIn(other, audit['selection']['concept_policy']['lexical_ids'])

    def test_policy_rollback_retains_rows_and_rejects_reinterpreted_event(self):
        self.add('A rare fluorescence phenomenon is measured.', ['instrument'])
        result, _ = self.retrieve('fluorescence', event_id='event')
        self.assertTrue(result)
        original = [tuple(r) for r in self.db.execute('SELECT * FROM memory_records')]
        legacy = MemoryGraph(self.db, retrieval_policy='legacy_v1')
        self.assertEqual(legacy.retrieve('scope', [], 'fluorescence', 3, event_id='old-policy'), [])
        with self.assertRaisesRegex(ValueError, 'selection policy'):
            legacy.retrieve('scope', [], 'fluorescence', 3, event_id='event')
        self.assertEqual(original, [tuple(r) for r in self.db.execute('SELECT * FROM memory_records')])

    def test_hash_valid_score_tampering_fails_arithmetic_validation(self):
        self.add('The photon yield is measured.', ['photon yield'])
        _, audit = self.retrieve('photon yield')
        changed = deepcopy(audit)
        changed['selection']['ranked_candidates'][0]['relevance_score'] += 1
        changed['durable_payload_sha256'] = digest({k: v for k, v in changed.items() if k != 'durable_payload_sha256'})
        with self.assertRaisesRegex(ValueError, 'concept score'):
            validate_graph_audit(changed)

    def test_literal_lookup_and_normalized_alias_do_not_mutate_graph_weights(self):
        self.add('Alpha beta electronic temperature.', ['alpha', 'beta', 'electronic temperature'])
        before = {t: [tuple(r) for r in self.db.execute('SELECT * FROM ' + t)]
                  for t in ('memory_static', 'memory_support', 'memory_dynamic')}
        self.retrieve('ALPHA beta electronic-temperature')
        after = {t: [tuple(r) for r in self.db.execute('SELECT * FROM ' + t)] for t in before}
        self.assertEqual(before, after)

    def test_missing_derived_trie_rebuilds_at_startup_not_during_query(self):
        self.add('Electronic temperature.', ['electronic temperature'])
        self.db.execute('DROP TABLE memory_coverage_trie')
        self.db.commit()
        with self.assertRaises(ValueError):
            self.graph.retrieve('scope', [], 'electronic-temperature', 3)
        self.graph = MemoryGraph(self.db, retrieval_policy='concept_v1')
        result, _ = self.retrieve('electronic-temperature')
        self.assertTrue(result)

    def test_missing_event_identities_fail_closed_without_changing_originals(self):
        self.add('Electronic temperature.', ['electronic temperature'])
        self.retrieve('electronic temperature')
        before = [tuple(r) for r in self.db.execute('SELECT * FROM memory_records')]
        self.db.execute('DROP TABLE memory_coverage_events')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'identities missing'):
            MemoryGraph(self.db, retrieval_policy='concept_v1')
        self.assertEqual(before, [tuple(r) for r in self.db.execute('SELECT * FROM memory_records')])


if __name__ == '__main__':
    unittest.main()
