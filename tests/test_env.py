"""S1.1 credential autoload: parse_env_text / load_env, on strings only (no files).

Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import pathlib
import unittest

from _support import cp

URL, MAIL, TOK = cp.CRED_VARS


class ParseEnvText(unittest.TestCase):
    def test_accepted_forms(self):
        text = (
            "# a comment\n"
            "\n"
            "PLAIN=value\n"
            "export EXPORTED=yes\n"
            "  SPACED = padded  \n"
            "DQ=\"two words # not a comment\"\n"
            "SQ='lit $HOME \\n'\n"
            "TRAIL=abc # trailing comment\n"
            "HASH=a#b\n"
            "ESC=\"say \\\"hi\\\" \\\\ ok\\n\"\n"
            "EQ=a=b=c\n"
            "EMPTY=\n"
            "not an assignment\n"
            "export\n"
        )
        self.assertEqual(cp.parse_env_text(text), {
            "PLAIN": "value", "EXPORTED": "yes", "SPACED": "padded",
            "DQ": "two words # not a comment", "SQ": "lit $HOME \\n", "TRAIL": "abc",
            "HASH": "a#b", "ESC": 'say "hi" \\ ok\n', "EQ": "a=b=c", "EMPTY": "",
        })

    def test_never_expands_or_executes(self):
        self.assertEqual(cp.parse_env_text("A=$(rm -rf /)\nB=`id`\nC=${HOME}\n"),
                         {"A": "$(rm -rf /)", "B": "`id`", "C": "${HOME}"})

    def test_later_assignment_wins(self):
        self.assertEqual(cp.parse_env_text("A=1\nA=2\n"), {"A": "2"})


def files(mapping):
    """A fake ``read_file``: ``{path: text}`` or ``{path: (text, mode)}``; others are missing."""
    def read(path):
        v = mapping.get(str(path))
        if v is None:
            return None
        return v if isinstance(v, tuple) else (v, 0o100600)
    return read


ENV = {"HOME": "/home/u"}
CWD = "/work"
USER = "/home/u/.config/confluence-publisher/.env"


def load(fs, environ=ENV, env_file=None, warn=None):
    warnings = []
    out = cp.load_env(env_file=env_file, cwd=CWD, environ=dict(environ), read_file=files(fs),
                      warn=warn or warnings.append)
    return out, warnings


class LoadEnv(unittest.TestCase):
    def test_search_paths(self):
        self.assertEqual(cp.env_search_paths("~/x.env", CWD, None, ENV)[1:],
                         [pathlib.Path("/work/.env"), pathlib.Path(USER)])
        self.assertEqual(cp.env_search_paths(None, CWD, None, {"XDG_CONFIG_HOME": "/xdg"})[-1],
                         pathlib.Path("/xdg/confluence-publisher/.env"))

    def test_first_hit_wins_per_variable(self):
        fs = {"/explicit.env": f"{URL}=https://explicit/wiki\n",
              "/work/.env": f"{URL}=https://cwd/wiki\n{MAIL}=cwd@x\n",
              USER: f"{URL}=https://user/wiki\n{MAIL}=user@x\n{TOK}=user-token\n"}
        out, _ = load(fs, environ={**ENV, MAIL: "env@x"}, env_file="/explicit.env")
        self.assertEqual(out, {URL: ("https://explicit/wiki", "/explicit.env"),
                               MAIL: ("env@x", "environment"),
                               TOK: ("user-token", USER)})

    def test_env_vars_win_set_a_workflow_unchanged(self):
        environ = {**ENV, URL: "u", MAIL: "m", TOK: "t"}
        out, _ = load({"/work/.env": f"{TOK}=file\n"}, environ=environ)
        self.assertEqual({k: v[1] for k, v in out.items()}, dict.fromkeys(cp.CRED_VARS, "environment"))

    def test_empty_env_var_falls_through_to_files(self):
        out, _ = load({"/work/.env": f"{TOK}=file\n"}, environ={**ENV, TOK: ""})
        self.assertEqual(out[TOK], ("file", "/work/.env"))

    def test_missing_env_file_is_an_error_naming_it(self):
        with self.assertRaises(FileNotFoundError) as cm:
            load({}, environ={**ENV, URL: "u", MAIL: "m", TOK: "t"}, env_file="/nope.env")
        self.assertEqual(cm.exception.filename, "/nope.env")
        self.assertIn("/nope.env", str(cm.exception))

    def test_missing_implicit_files_are_not_an_error(self):
        self.assertEqual(load({}), ({}, []))

    def test_group_readable_token_file_warns_but_loads(self):
        out, warnings = load({"/work/.env": (f"{TOK}=t\n", 0o100644)})
        self.assertEqual(out[TOK], ("t", "/work/.env"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("chmod 600 /work/.env", warnings[0])
        self.assertNotIn("=t", warnings[0])

    def test_no_warning_for_private_file_or_non_token_file(self):
        _, w1 = load({"/work/.env": (f"{TOK}=t\n", 0o100600)})
        _, w2 = load({"/work/.env": (f"{URL}=u\n", 0o100644)})
        self.assertEqual((w1, w2), ([], []))

    def test_only_credential_vars_are_loaded(self):
        out, _ = load({"/work/.env": "PATH=/evil\nOTHER=x\n"})
        self.assertEqual(out, {})

    def test_apply_env_fills_only_unset(self):
        environ = {MAIL: "keep"}
        cp.apply_env({URL: ("u", "/f"), MAIL: ("m", "/f")}, environ)
        self.assertEqual(environ, {URL: "u", MAIL: "keep"})


if __name__ == "__main__":
    unittest.main()
