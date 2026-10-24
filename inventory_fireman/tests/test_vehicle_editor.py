# -*- coding: utf-8 -*-
import re

from odoo.tests import HttpCase, tagged


@tagged("inventory_fireman")
class TestVehicleEditor(HttpCase):
    """Éditeur de véhicule du portail : insertion sous une ligne et glisser-déposer."""

    def setUp(self):
        super().setUp()
        env = self.env(context=dict(self.env.context, fireman_no_push=True))
        self.user = env["res.users"].create(
            {
                "name": "Admin portail",
                "login": "portal-editor",
                "password": "portal-editor-pw",
                "email": "portal-editor@example.com",
                "groups_id": [(6, 0, [env.ref("base.group_portal").id])],
            }
        )
        self.uo = env["fireman.uo"].create({"name": "Editeur", "code": "TED"})
        env["fireman.member"].create(
            {"uo_id": self.uo.id, "partner_id": self.user.partner_id.id, "is_admin": True, "verified": True}
        )
        self.vehicle = env["fireman.vehicle"].create({"uo_id": self.uo.id, "name": "VSAV"})
        self.section = env["fireman.vehicle.section"].create({"vehicle_id": self.vehicle.id, "name": "Sac"})
        self.items = env["fireman.vehicle.item"].create(
            [
                {"section_id": self.section.id, "name": name, "quantity": 1, "sequence": seq}
                for name, seq in (("A", 0), ("B", 10), ("C", 20))
            ]
        )
        self.url = f"/my/uo/{self.uo.id}/vehicles/{self.vehicle.id}"
        self.authenticate("portal-editor", "portal-editor-pw")
        page = self.url_open(self.url).text
        self.page = page
        self.token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def _post(self, **data):
        return self.url_open(
            f"{self.url}/edit",
            data={"csrf_token": self.token, **data},
            headers={"X-Requested-With": "fetch"},
        )

    def _names(self):
        self.section.invalidate_recordset()
        return self.section.item_ids.sorted(lambda i: (i.sequence, i.id)).mapped("name")

    def test_admin_page_has_handles_and_insert_buttons(self):
        self.assertIn("fm-grip", self.page)
        self.assertIn('data-sortable="1"', self.page)
        self.assertIn('name="after_id"', self.page)

    def test_reorder_applies_the_dropped_order(self):
        a, b, c = self.items
        response = self._post(action="reorder", kind="item", ids=f"{c.id},{a.id},{b.id}")
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self._names(), ["C", "A", "B"])

    def test_reorder_rejects_a_partial_or_foreign_list(self):
        a, b, _c = self.items
        response = self._post(action="reorder", kind="item", ids=f"{b.id},{a.id}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._names(), ["A", "B", "C"])

    def test_item_is_inserted_right_after_the_chosen_line(self):
        a, _b, _c = self.items
        self._post(action="add_item", after_id=a.id, name="Nouveau", quantity="2")
        self.assertEqual(self._names(), ["A", "Nouveau", "B", "C"])
        self.assertEqual(self.section.item_ids.filtered(lambda i: i.name == "Nouveau").quantity, 2)

    def test_item_inserted_after_the_last_line_stays_last(self):
        _a, _b, c = self.items
        self._post(action="add_item", after_id=c.id, name="Fin")
        self.assertEqual(self._names(), ["A", "B", "C", "Fin"])
