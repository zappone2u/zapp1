# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged

from ..models.fireman_vehicle_template import normalize_structure

SECTIONS = [
    {
        "label": "Cellule",
        "items": [
            {"description": "Brancard", "quantity": 1},
            {"label": "Trousse", "sub_items": [{"description": "Compresses", "quantity": 12}]},
        ],
    },
    {"label": "Coffre", "items": [{"description": "Cale", "quantity": 2}]},
]


class FakeRef:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def set(self, value):
        self.store[self.path] = value

    def delete(self):
        self.store[self.path] = None

    def get(self):
        return self.store.get(self.path)


class FakeFirebase:
    """Realtime Database réduite aux chemins écrits ou lus par les modèles."""

    def __init__(self, **nodes):
        self.store = dict(nodes)

    def rtdb(self, path="/"):
        return FakeRef(self.store, path)


@tagged("inventory_fireman")
class TestTemplateSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env["fireman.vehicle.template"]
        cls.Event = cls.env["fireman.sync.event"]
        cls.service = cls.env["fireman.sync.service"].with_context(fireman_no_push=True)
        cls.uo = cls.env["fireman.uo"].create({"name": "UO modèles", "code": "TM1"})

    def _pull(self, raw, blocked=()):
        self.service._apply_templates(raw, set(blocked))

    def _mine(self, uid):
        return self.Template.with_context(active_test=False).search([("uid", "=", uid)])

    # -- normalisation --------------------------------------------------
    def test_normalize_structure(self):
        self.assertEqual(normalize_structure(SECTIONS), SECTIONS)
        raw = {
            "0": {
                "label": " A ",
                "items": {"0": {"description": "x", "quantity": "oups"}, "2": {"description": "z", "quantity": -3}},
            },
            "1": {"label": "", "items": []},
            "2": "non",
        }
        self.assertEqual(
            normalize_structure(raw),
            [{"label": "A", "items": [{"description": "x", "quantity": 1}, {"description": "z", "quantity": 1}]}],
        )
        self.assertEqual(
            normalize_structure([{"label": "A", "items": [{"description": "x", "quantity": 0}]}])[0]["items"][0][
                "quantity"
            ],
            0,
        )
        self.assertEqual(normalize_structure(None), [])

    # -- Firebase → Odoo -------------------------------------------------
    def test_pull_creates_a_template(self):
        self._pull({"vsav": {"label": "VSAV", "description": "Complet", "sections": SECTIONS}})
        template = self._mine("vsav")
        self.assertEqual((template.name, template.description), ("VSAV", "Complet"))
        self.assertFalse(template.uo_id)
        self.assertTrue(template.in_firebase)
        self.assertEqual(template._structure(), SECTIONS)

    def test_pull_is_idempotent(self):
        raw = {"vsav": {"label": "VSAV", "sections": SECTIONS}}
        self._pull(raw)
        template = self._mine("vsav")
        write_date, sections = template.write_date, template.section_ids
        self._pull(raw)
        self.assertEqual(template.write_date, write_date)
        self.assertEqual(template.section_ids, sections)

    def test_pull_updates_content_and_reservation(self):
        self._pull({"vsav": {"label": "VSAV", "sections": SECTIONS}})
        self._pull({"vsav": {"label": "VSAV 2", "uo": "TM1", "sections": SECTIONS[1:]}})
        template = self._mine("vsav")
        self.assertEqual((template.name, template.uo_id), ("VSAV 2", self.uo))
        self.assertEqual(template._structure(), SECTIONS[1:])
        self._pull({"vsav": {"label": "VSAV 2", "sections": SECTIONS[1:]}})
        self.assertFalse(template.uo_id)

    def test_pull_ignores_unusable_entries(self):
        self._pull(
            {
                "texte": "non",
                "sans_nom": {"sections": SECTIONS},
                "uo_inconnue": {"label": "X", "uo": "ZZZ", "sections": SECTIONS},
            }
        )
        self.assertFalse(self._mine("texte") | self._mine("sans_nom") | self._mine("uo_inconnue"))

    def test_removed_from_firebase_is_removed_from_odoo(self):
        self._pull({"vsav": {"label": "VSAV", "sections": SECTIONS}})
        self._pull({})
        self.assertFalse(self._mine("vsav"))

    def test_odoo_only_template_is_never_removed_by_a_pull(self):
        template = self.Template.from_structure("Créé dans Odoo", SECTIONS)
        self.assertFalse(template.in_firebase)
        self._pull(None)
        self.assertTrue(template.exists())

    def test_template_waiting_to_be_pushed_is_not_overwritten(self):
        self._pull({"vsav": {"label": "VSAV", "sections": SECTIONS}})
        template = self._mine("vsav")
        self._pull(
            {"vsav": {"label": "Ancien nom", "sections": []}},
            blocked={(self.Template._name, f"vehicle_templates/{template.uid}")},
        )
        self.assertEqual(template.name, "VSAV")
        self.assertEqual(template._structure(), SECTIONS)
        self._pull({}, blocked={(self.Template._name, f"vehicle_templates/{template.uid}")})
        self.assertTrue(template.exists())

    # -- Odoo → Firebase -------------------------------------------------
    def test_new_template_gets_a_key_and_is_queued_once(self):
        self.Event.search([]).unlink()
        template = self.Template.from_structure("VSAV", SECTIONS)
        self.assertTrue(template.uid)
        events = self.Event.search(
            [("direction", "=", "push"), ("target_key", "=", f"vehicle_templates/{template.uid}")]
        )
        self.assertEqual(len(events), 1)

    def test_editing_an_item_queues_the_template(self):
        template = self.Template.from_structure("VSAV", SECTIONS)
        self.Event.search([]).unlink()
        template.section_ids[0].item_ids[0].name = "Autre"
        events = self.Event.search(
            [("direction", "=", "push"), ("target_key", "=", f"vehicle_templates/{template.uid}")]
        )
        self.assertEqual(len(events), 1)

    def test_push_writes_the_firebase_format(self):
        template = self.Template.from_structure("VSAV", SECTIONS, uo=self.uo)
        template.description = "Complet"
        firebase = FakeFirebase()
        template._push_upsert(firebase)
        self.assertEqual(
            firebase.store[f"vehicle_templates/{template.uid}"],
            {"label": "VSAV", "description": "Complet", "uo": "TM1", "sections": SECTIONS},
        )
        self.assertTrue(template.in_firebase)

    def test_push_of_an_archived_template_removes_it(self):
        template = self.Template.from_structure("VSAV", SECTIONS)
        firebase = FakeFirebase()
        template._push_upsert(firebase)
        template.active = False
        template._push_upsert(firebase)
        self.assertIsNone(firebase.store[f"vehicle_templates/{template.uid}"])
        self.assertFalse(template.in_firebase)

    def test_deleting_a_template_queues_its_removal(self):
        template = self.Template.from_structure("VSAV", SECTIONS)
        uid = template.uid
        self.Event.search([]).unlink()
        template.unlink()
        event = self.Event.search([("direction", "=", "push"), ("op", "=", "delete")])
        self.assertEqual(event.target_key, f"vehicle_templates/{uid}")
        firebase = FakeFirebase()
        self.Template._push_delete(firebase, {"uid": uid})
        self.assertIsNone(firebase.store[f"vehicle_templates/{uid}"])

    def test_what_odoo_pushes_is_read_back_unchanged(self):
        template = self.Template.from_structure("VSAV", SECTIONS, uo=self.uo)
        firebase = FakeFirebase()
        template._push_upsert(firebase)
        before = template._structure()
        self._pull({template.uid: firebase.store[f"vehicle_templates/{template.uid}"]})
        self.assertEqual(template._structure(), before)
        self.assertEqual(template.uo_id, self.uo)

    def test_pull_templates_reads_the_global_node(self):
        firebase = FakeFirebase(vehicle_templates={"vsav": {"label": "VSAV", "sections": SECTIONS}})
        self.service.pull_templates(connector=firebase)
        self.assertEqual(self._mine("vsav")._structure(), SECTIONS)
