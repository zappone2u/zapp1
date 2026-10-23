# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.utils import QuotaExceeded

STRUCTURE = [
    {
        "label": "Cellule",
        "items": [
            {"description": "Brancard", "quantity": 1},
            {"label": "Trousse", "sub_items": [{"description": "Compresses", "quantity": 12}]},
        ],
    },
    {"label": "Coffre", "items": [{"description": "Cale", "quantity": 2}]},
]


class VehicleCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Uo = cls.env["fireman.uo"]
        cls.Vehicle = cls.env["fireman.vehicle"]
        cls.Template = cls.env["fireman.vehicle.template"]
        cls.uo = cls.Uo.create({"name": "UO modèles", "code": "TV1"})
        cls.other = cls.Uo.create({"name": "Autre UO", "code": "TV2"})
        cls.template = cls.Template.from_structure("VSAV test", STRUCTURE)

    def _vehicle_names(self, uo):
        return uo.vehicle_ids.mapped("name")


@tagged("inventory_fireman")
class TestVehicleTemplate(VehicleCase):
    def test_structure_round_trip(self):
        self.assertEqual(self.template._structure(), STRUCTURE)
        self.assertEqual((self.template.section_count, self.template.item_count), (2, 3))

    def test_create_vehicle_copies_the_content_with_new_ids(self):
        vehicle = self.template.create_vehicle(self.uo, "VSAV 1")
        self.assertEqual(vehicle._structure(), STRUCTURE)
        self.assertEqual((vehicle.uo_id, vehicle.name), (self.uo, "VSAV 1"))
        self.assertEqual((vehicle.license_plate, vehicle.notes, vehicle.status), (False, False, "nothing"))
        self.assertFalse(vehicle.verified)
        uids = [s["id"] for s in vehicle._firebase_products()]
        self.assertEqual(len(set(uids)), 2)

    def test_two_vehicles_from_the_same_template_are_independent(self):
        template = self.template.with_context(fireman_skip_quota=True)
        first = template.create_vehicle(self.uo, "A")
        second = template.create_vehicle(self.uo, "B")
        first.section_ids[0].name = "Modifiée"
        self.assertEqual(second._structure(), STRUCTURE)
        self.assertEqual(self.template._structure(), STRUCTURE)
        first_ids = {i.uid for i in first.section_ids.item_ids}
        second_ids = {i.uid for i in second.section_ids.item_ids}
        self.assertFalse(first_ids & second_ids)

    def test_vehicle_is_queued_for_firebase_once(self):
        self.env["fireman.sync.event"].search([]).unlink()
        vehicle = self.template.create_vehicle(self.uo, "VSAV 1")
        events = self.env["fireman.sync.event"].search(
            [("direction", "=", "push"), ("target_key", "=", f"{self.uo.code}/{vehicle.uid}")]
        )
        self.assertEqual(len(events), 1)

    def test_quota_is_checked_and_nothing_is_left_behind(self):
        self.template.create_vehicle(self.uo, "VSAV 1")  # le palier Découverte autorise 1 véhicule
        sections_before = self.env["fireman.vehicle.section"].search_count([])
        try:
            self.template.create_vehicle(self.uo, "VSAV 2")
        except QuotaExceeded:
            pass
        else:
            self.fail("QuotaExceeded attendue")
        self.assertEqual(self._vehicle_names(self.uo), ["VSAV 1"])
        self.assertEqual(self.env["fireman.vehicle.section"].search_count([]), sections_before)

    def test_reserved_template_is_only_available_to_its_uo(self):
        mine = self.Template.from_structure("Réservé", STRUCTURE, uo=self.uo)
        self.assertIn(mine, self.Template._available_for(self.uo))
        self.assertNotIn(mine, self.Template._available_for(self.other))
        self.assertIn(self.template, self.Template._available_for(self.other), "un modèle partagé reste visible")
        with self.assertRaises(ValidationError):
            mine.create_vehicle(self.other, "Intrus")
        self.assertFalse(self.other.vehicle_ids)

    def test_archived_template_is_not_offered(self):
        self.template.active = False
        self.assertNotIn(self.template, self.Template._available_for(self.uo))

    def test_save_a_vehicle_as_a_template_reserved_to_its_uo(self):
        vehicle = self.template.create_vehicle(self.uo, "VSAV 1")
        action = vehicle.action_save_as_template()
        saved = self.Template.browse(action["res_id"])
        self.assertEqual((saved.uo_id, saved.name), (self.uo, "VSAV 1"))
        self.assertEqual(saved._structure(), STRUCTURE)
        self.assertNotIn(saved, self.Template._available_for(self.other))

    def test_api_payload(self):
        payload = self.template._api_payload()
        self.assertEqual(payload["label"], "VSAV test")
        self.assertTrue(payload["shared"])
        self.assertEqual(payload["sections"], STRUCTURE)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_shipped_vsav_template(self):
        shipped = self.env.ref("inventory_fireman.vehicle_template_vsav")
        self.assertFalse(shipped.uo_id)
        self.assertEqual((shipped.section_count, shipped.item_count), (26, 210))

    def test_negative_quantity_is_refused(self):
        item = self.template.section_ids[0].item_ids[0]
        with self.assertRaises(ValidationError):
            item.quantity = -1


@tagged("inventory_fireman")
class TestVehicleDuplicate(VehicleCase):
    def setUp(self):
        super().setUp()
        self.vehicle = self.template.create_vehicle(self.uo, "VSAV 1")
        self.vehicle.write({"license_plate": "AB-123-CD", "notes": "feu HS", "status": "broken", "verified": True})
        self.uo.invalidate_recordset()

    def test_duplicate_is_refused_when_the_quota_is_reached(self):
        before = self.Vehicle.search_count([])
        try:
            self.vehicle.duplicate_vehicle()
        except QuotaExceeded:
            pass
        else:
            self.fail("QuotaExceeded attendue")
        self.assertEqual(self.Vehicle.search_count([]), before)

    def test_duplicate_copies_only_the_equipment(self):
        copy = self.vehicle.with_context(fireman_skip_quota=True).duplicate_vehicle("VSAV 2")
        self.assertEqual(copy._structure(), self.vehicle._structure())
        self.assertEqual((copy.name, copy.uo_id), ("VSAV 2", self.uo))
        self.assertEqual((copy.license_plate, copy.notes, copy.status), (False, False, "nothing"))
        self.assertFalse(copy.verified)
        self.assertNotEqual(copy.uid, self.vehicle.uid)
        self.assertFalse(
            {i.uid for i in copy.section_ids.item_ids} & {i.uid for i in self.vehicle.section_ids.item_ids}
        )

    def test_duplicate_default_name(self):
        copy = self.vehicle.with_context(fireman_skip_quota=True).duplicate_vehicle()
        self.assertEqual(copy.name, "VSAV 1 (copie)")

    def test_source_vehicle_is_unchanged(self):
        self.vehicle.with_context(fireman_skip_quota=True).duplicate_vehicle()
        self.assertEqual(self.vehicle.license_plate, "AB-123-CD")
        self.assertEqual(self.vehicle._structure(), STRUCTURE)

    def test_archived_vehicles_do_not_count(self):
        self.vehicle.active = False
        self.uo.invalidate_recordset()
        copy = self.vehicle.duplicate_vehicle("Remplaçant")
        self.assertEqual(self._vehicle_names(self.uo), ["Remplaçant"])
        self.assertEqual(copy._structure(), STRUCTURE)


class FakeConnector:
    """Remplace Firebase : le jeton est l'uid, et les comptes sont décrits en dur."""

    def __init__(self, users):
        self.users = users

    def verify_id_token(self, token):
        if token not in self.users:
            raise ValueError("jeton invalide")
        return {"uid": token}

    def get_user(self, uid):
        return self.users[uid]


@tagged("post_install", "-at_install", "inventory_fireman")
class TestTemplatesApi(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Uo, Template = cls.env["fireman.uo"], cls.env["fireman.vehicle.template"]
        cls.uo = Uo.create({"name": "UO api", "code": "TA1"})
        cls.other = Uo.create({"name": "UO autre", "code": "TA2"})
        cls.shared = Template.from_structure("Partagé API", STRUCTURE)
        cls.mine = Template.from_structure("Réservé TA1", STRUCTURE, uo=cls.uo)
        cls.theirs = Template.from_structure("Réservé TA2", STRUCTURE, uo=cls.other)
        cls.users = {
            "member": {"uo": [{"uo_name": "TA1"}]},
            "outsider": {"uo": [{"uo_name": "TA2"}]},
            "superadmin": {"admin": True, "uo": []},
        }

    def _call(self, uo="TA1", token="member", body=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        connector = FakeConnector(self.users)
        with patch.object(type(self.env["firebase.connector"]), "_get_default", lambda self_: connector):
            return self.url_open(
                "/fireman/api/v1/templates", data=json.dumps(body if body is not None else {"uo": uo}), headers=headers
            )

    def test_member_gets_shared_and_own_uo_templates_only(self):
        response = self._call()
        self.assertEqual(response.status_code, 200)
        names = {t["label"] for t in response.json()["templates"]}
        self.assertIn("Partagé API", names)
        self.assertIn("Réservé TA1", names)
        self.assertNotIn("Réservé TA2", names)

    def test_member_of_another_uo_is_refused(self):
        self.assertEqual(self._call(token="outsider").status_code, 403)

    def test_super_admin_can_read_any_uo(self):
        self.assertEqual(self._call(token="superadmin").status_code, 200)

    def test_missing_or_invalid_token(self):
        self.assertEqual(self._call(token=None).status_code, 401)
        self.assertEqual(self._call(token="inconnu").status_code, 401)

    def test_invalid_uo_code(self):
        self.assertEqual(self._call(body={"uo": "a b"}).status_code, 400)
        self.assertEqual(self._call(body={}).status_code, 400)


@tagged("post_install", "-at_install", "inventory_fireman")
class TestPortalMembers(HttpCase):
    """Les super administrateurs ne doivent jamais apparaître dans l'espace client."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        # Les super administrateurs rejoignent chaque UO à sa création : ils doivent exister avant elle.
        cls.supers = Partner.create(
            [
                {
                    "name": f"Supernom{i}",
                    "email": f"super{i}@secret.example",
                    "firebase_uid": f"s{i}",
                    "fireman_super_admin": True,
                }
                for i in range(3)
            ]
        )
        cls.uo = cls.env["fireman.uo"].create({"name": "UO portail", "code": "TPM"})
        cls.customer = cls.env["res.users"].create(
            {
                "name": "Cliente Admin",
                "login": "cliente@example.com",
                "password": "cliente-pass",
                "groups_id": [(6, 0, [cls.env.ref("base.group_portal").id])],
            }
        )
        cls.colleague = Partner.create(
            {"name": "Collegue Visible", "email": "collegue@example.com", "firebase_uid": "c1"}
        )
        cls.env["fireman.member"].with_context(fireman_skip_quota=True).create(
            [
                {"uo_id": cls.uo.id, "partner_id": cls.customer.partner_id.id, "is_admin": True, "verified": True},
                {"uo_id": cls.uo.id, "partner_id": cls.colleague.id, "verified": True},
            ]
        )
        cls.uo.invalidate_recordset()

    def test_super_admins_are_members_but_never_shown(self):
        self.assertEqual(set(self.supers) <= set(self.uo.member_ids.partner_id), True, "ils sont bien membres")
        self.authenticate("cliente@example.com", "cliente-pass")
        response = self.url_open(f"/my/uo/{self.uo.id}/members")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Collegue Visible", html)
        for partner in self.supers:
            self.assertNotIn(partner.name, html)
            self.assertNotIn(partner.email, html)
        self.assertNotIn("Super administrateur", html)

    def test_visible_members_helper(self):
        from ..controllers.portal import FiremanPortal

        visible = FiremanPortal()._visible_members(self.uo)
        self.assertFalse(visible.filtered("is_super_admin"))
        self.assertEqual(set(visible.partner_id), {self.customer.partner_id, self.colleague})

    def test_a_super_admin_cannot_be_modified_from_the_portal(self):
        self.authenticate("cliente@example.com", "cliente-pass")
        member = self.uo.member_ids.filtered(lambda m: m.partner_id == self.supers[0])
        self.assertTrue(member)
        csrf = self.url_open(f"/my/uo/{self.uo.id}/members").text.split('name="csrf_token" value="')[1].split('"')[0]
        response = self.url_open(
            f"/my/uo/{self.uo.id}/members/{member.id}/remove", data={"csrf_token": csrf}, allow_redirects=False
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(member.exists())

    def test_inviting_a_super_admin_reveals_nothing(self):
        self.authenticate("cliente@example.com", "cliente-pass")
        csrf = self.url_open(f"/my/uo/{self.uo.id}/members").text.split('name="csrf_token" value="')[1].split('"')[0]
        connector = FakeConnector({})
        connector.find_user_by_email = lambda email: (self.supers[0].firebase_uid, {})
        with patch.object(type(self.env["firebase.connector"]), "_get_default", lambda self_: connector):
            response = self.url_open(
                f"/my/uo/{self.uo.id}/members/invite",
                data={"csrf_token": csrf, "email": self.supers[0].email},
                allow_redirects=False,
            )
        self.assertIn("ok=invited", response.headers["Location"])
        self.assertNotIn("error=already_member", response.headers["Location"])


@tagged("post_install", "-at_install", "inventory_fireman")
class TestPortalVehicles(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uo = cls.env["fireman.uo"].create({"name": "UO véhicules", "code": "TPV"})
        cls.template = cls.env["fireman.vehicle.template"].from_structure("Modèle portail", STRUCTURE)
        cls.hidden = cls.env["fireman.vehicle.template"].from_structure(
            "Modèle d'une autre UO",
            STRUCTURE,
            uo=cls.env["fireman.uo"].create({"name": "Autre", "code": "TPW"}),
        )
        cls.customer = cls.env["res.users"].create(
            {
                "name": "Admin véhicules",
                "login": "vehicules@example.com",
                "password": "vehicules-pass",
                "groups_id": [(6, 0, [cls.env.ref("base.group_portal").id])],
            }
        )
        cls.env["fireman.member"].with_context(fireman_skip_quota=True).create(
            {"uo_id": cls.uo.id, "partner_id": cls.customer.partner_id.id, "is_admin": True, "verified": True}
        )

    def _csrf(self):
        page = self.url_open(f"/my/uo/{self.uo.id}/vehicles").text
        return page.split('name="csrf_token" value="')[1].split('"')[0], page

    def _post(self, path, **data):
        token, _page = self._csrf()
        return self.url_open(path, data={"csrf_token": token, **data}, allow_redirects=False)

    def setUp(self):
        super().setUp()
        self.authenticate("vehicules@example.com", "vehicules-pass")

    def test_selector_offers_templates_of_the_uo_only(self):
        _token, page = self._csrf()
        self.assertIn("Modèle portail", page)
        self.assertNotIn("Modèle d'une autre UO", page)

    def test_create_from_a_template(self):
        response = self._post(
            f"/my/uo/{self.uo.id}/vehicles/new",
            name="VSAV portail",
            license_plate="ZZ-999-ZZ",
            source=f"template:{self.template.id}",
        )
        self.assertIn("ok=created", response.headers["Location"])
        vehicle = self.uo.vehicle_ids
        self.assertEqual((vehicle.name, vehicle.license_plate), ("VSAV portail", "ZZ-999-ZZ"))
        self.assertEqual(vehicle._structure(), STRUCTURE)

    def test_template_of_another_uo_is_refused(self):
        response = self._post(f"/my/uo/{self.uo.id}/vehicles/new", name="Intrus", source=f"template:{self.hidden.id}")
        self.assertIn("error=unknown_source", response.headers["Location"])
        self.assertFalse(self.uo.vehicle_ids)

    def test_invalid_source_is_refused(self):
        for source in ("template:abc", "vehicle:999999", "autre:1"):
            response = self._post(f"/my/uo/{self.uo.id}/vehicles/new", name="X", source=source)
            self.assertIn("error=unknown_source", response.headers["Location"], source)
        self.assertFalse(self.uo.vehicle_ids)

    def test_create_over_quota_goes_to_the_subscription_page(self):
        self._post(f"/my/uo/{self.uo.id}/vehicles/new", name="Premier", source=f"template:{self.template.id}")
        response = self._post(f"/my/uo/{self.uo.id}/vehicles/new", name="Second", source=f"template:{self.template.id}")
        self.assertIn(f"/my/uo/{self.uo.id}/subscription", response.headers["Location"])
        self.assertIn("error=quota", response.headers["Location"])
        self.assertEqual(self.uo.vehicle_ids.mapped("name"), ["Premier"])

    def test_duplicate_over_quota_is_refused(self):
        first = self.template.create_vehicle(self.uo, "Original")
        response = self._post(f"/my/uo/{self.uo.id}/vehicles/{first.id}/duplicate", name="Copie")
        self.assertIn("error=quota", response.headers["Location"])
        self.assertEqual(self.uo.vehicle_ids, first)

    def test_duplicate_a_vehicle_of_another_uo_is_not_found(self):
        other_vehicle = self.template.create_vehicle(self.hidden.uo_id, "Autre véhicule")
        response = self._post(f"/my/uo/{self.uo.id}/vehicles/{other_vehicle.id}/duplicate", name="Vol")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.uo.vehicle_ids)
