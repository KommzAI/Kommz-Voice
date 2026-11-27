"""Essai gratuit : un identifiant d'appareil commun à plusieurs PC est refusé.

vtp_web_server.py ne s'importe pas sans ses variables d'environnement (Supabase,
secrets). Les fonctions sont donc extraites avec `ast` et exécutées seules.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "vtp_web_server.py")

with open(SOURCE, encoding="utf-8") as fh:
    TEXTE = fh.read()
ARBRE = ast.parse(TEXTE, filename="vtp_web_server.py")


def _affectation(nom):
    for noeud in ARBRE.body:
        if isinstance(noeud, ast.Assign) and len(noeud.targets) == 1:
            cible = noeud.targets[0]
            if isinstance(cible, ast.Name) and cible.id == nom:
                return noeud
    raise LookupError(nom)


def _fonction(nom):
    for noeud in ARBRE.body:
        if isinstance(noeud, ast.FunctionDef) and noeud.name == nom:
            return noeud
    raise LookupError(nom)


def _charger():
    espace = {"re": re}
    # HWID_RE : on relit le motif tel qu'il est écrit dans le serveur
    motif = ast.literal_eval(_affectation("HWID_RE").value.args[0])
    espace["HWID_RE"] = re.compile(motif)
    for nom in ("_PLACEHOLDER_HWIDS",):
        noeud = _affectation(nom)
        exec(compile(ast.Module(body=[noeud], type_ignores=[]), SOURCE, "exec"), espace)
    for nom in ("_is_valid_hwid", "_is_placeholder_hwid"):
        exec(compile(ast.Module(body=[_fonction(nom)], type_ignores=[]), SOURCE, "exec"), espace)
    return espace


ESPACE = _charger()
valide = ESPACE["_is_valid_hwid"]
placeholder = ESPACE["_is_placeholder_hwid"]


@pytest.mark.parametrize("valeur", [
    "unknown_hwid", "UNKNOWN_HWID", "  unknown_hwid  ", "unknown", "none", "null", "undefined",
    "", None, "00000000", "FFFFFFFF", "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff", "aaaaaaaaaaaaaaaa",
])
def test_identifiants_communs_a_plusieurs_pc_sont_refuses(valeur):
    assert placeholder(valeur) is True


@pytest.mark.parametrize("valeur", [
    "4C4C4544-0042-3510-8053-B7C04F4D5732",
    "7b3a9f10c2d84e56a1b2c3d4e5f60718",   # md5 hexadécimal du client
    "a1b2c3d4e5f60718293a4b5c6d7e8f90",
])
def test_vrais_identifiants_sont_acceptes(valeur):
    assert placeholder(valeur) is False
    assert valide(valeur) is True


def test_le_serveur_acceptait_unknown_hwid_avant_le_filtre():
    """C'est le défaut d'origine : le motif seul laissait passer la valeur fixe."""
    assert valide("unknown_hwid") is True


def _garde_placeholder(route):
    """Le `if _is_placeholder_hwid(...)` de la route : le test doit être cet appel, tel quel."""
    for noeud in ast.walk(route):
        if (isinstance(noeud, ast.If) and isinstance(noeud.test, ast.Call)
                and getattr(noeud.test.func, "id", "") == "_is_placeholder_hwid"):
            return noeud
    return None


def test_la_route_d_essai_refuse_avant_toute_lecture_de_la_base():
    route = _fonction("activate_desktop_trial_license")
    garde = _garde_placeholder(route)
    assert garde is not None, "la route ne filtre pas les identifiants communs"
    assert any(isinstance(n, ast.Return) for n in garde.body), "le filtre doit renvoyer une erreur"
    premiere_lecture = min(
        n.lineno for n in ast.walk(route)
        if (isinstance(n, ast.Constant) and n.value == "trial_hwid_log")
        or (isinstance(n, ast.Name) and n.id == "supabase")
    )
    assert garde.lineno < premiere_lecture


def test_la_garde_renvoie_une_erreur_400():
    garde = _garde_placeholder(_fonction("activate_desktop_trial_license"))
    retour = next(n for n in ast.walk(garde) if isinstance(n, ast.Return))
    assert isinstance(retour.value, ast.Tuple) and retour.value.elts[1].value == 400


def test_le_message_d_erreur_demande_la_mise_a_jour():
    route = _fonction("activate_desktop_trial_license")
    corps = ast.get_source_segment(TEXTE, route)
    assert "Mettez a jour Kommz Gamer" in corps
