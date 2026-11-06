"""Paso DevOps: cambios pendientes, borrador del PR, git local y atajo desde main(); sin red ni modelo."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import integration
import main as app
from crew.workflow import workflow_steps

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}
DRAFT = "BRANCH: crewai/transfer-funds\nCOMMIT: Add transfer funds\nTITLE: feat: transfer funds\n---\n## Resumen\nCuerpo"


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PendingChangesTests(unittest.TestCase):
    def test_detects_new_modified_and_ignores_build_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write(out, "specs/user_story.md", "a")
            write(out, "backend-quarkus/target/classes/X.class", "x")
            write(out, "frontend-angular/node_modules/pkg/index.js", "x")
            write(out, ".checkpoint.json", "{}")
            self.assertEqual(integration.pending_changes(out), {"new": ["specs/user_story.md"], "modified": []})

            integration.save_manifest(out, integration.output_files(out), "url", "branch")
            self.assertEqual(integration.pending_changes(out), {"new": [], "modified": []})

            write(out, "specs/user_story.md", "b")
            write(out, "specs/devops_report.md", "report")
            self.assertEqual(integration.pending_changes(out), {"new": [], "modified": ["specs/user_story.md"]})


class DraftTests(unittest.TestCase):
    def test_parse_draft(self):
        draft = integration.parse_draft(DRAFT, "Transfer funds")
        self.assertEqual((draft.branch, draft.commit, draft.title), ("crewai/transfer-funds", "Add transfer funds", "feat: transfer funds"))
        self.assertEqual(draft.body, "## Resumen\nCuerpo")

    def test_invalid_branch_falls_back_to_safe_name(self):
        draft = integration.parse_draft("BRANCH: ../x; rm -rf\nTITLE: t\n---\nb", "Transfer Funds!")
        self.assertEqual(draft.branch, "crewai/transfer-funds")

    def test_repo_url_rejects_unsafe_names(self):
        self.assertEqual(integration.repo_url("acme", "ai-labs"), "https://github.com/acme/ai-labs.git")
        with self.assertRaises(ValueError):
            integration.repo_url("acme/../x", "ai-labs")


class GitTests(unittest.TestCase):
    def test_stage_commit_push_to_local_remote(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GIT_ENV):
            root = Path(tmp)
            remote, seed, out = root / "remote.git", root / "seed", root / "out"
            subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
            seed.mkdir()
            write(seed, "specs/user_story.md", "old")
            for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "init"],
                         ["remote", "add", "origin", str(remote)], ["push", "origin", "main"]):
                subprocess.run(["git", *args], cwd=seed, check=True, capture_output=True)

            write(out, "specs/user_story.md", "new")
            write(out, "backend-quarkus/pom.xml", "pom")
            staging = integration.stage_changes(out, integration.output_files(out), str(remote), "main")
            try:
                self.assertEqual(staging.added, ["backend-quarkus/pom.xml"])
                self.assertEqual(staging.modified, ["specs/user_story.md"])
                integration.commit_and_push(staging, "crewai/feature-1", "Add feature")
            finally:
                integration.cleanup(staging)
            self.assertFalse(staging.path.exists())
            branches = subprocess.run(["git", "branch", "--list"], cwd=remote, capture_output=True, text=True).stdout
            self.assertIn("crewai/feature-1", branches)

    def test_nothing_to_integrate_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GIT_ENV):
            root = Path(tmp)
            remote, seed, out = root / "remote.git", root / "seed", root / "out"
            subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
            seed.mkdir()
            write(seed, "a.md", "same")
            for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "init"],
                         ["remote", "add", "origin", str(remote)], ["push", "origin", "main"]):
                subprocess.run(["git", *args], cwd=seed, check=True, capture_output=True)
            write(out, "a.md", "same")
            with self.assertRaises(ValueError):
                integration.stage_changes(out, integration.output_files(out), str(remote), "main")


class MainShortcutTests(unittest.TestCase):
    def test_pending_changes_offer_devops_only_and_open_pr(self):
        steps = workflow_steps(False)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            out = Path(tmp) / "output"
            write(out, "specs/user_story.md", "story")
            write(out, "backend-quarkus/pom.xml", "pom")
            checkpoint = {
                "workflow_mode": "standard",
                "feature_title": "Transfer funds",
                "steps": {step[0]: {"status": "approved", "output": f"ok {step[0]}"} for step in steps},
            }
            write(out, ".checkpoint.json", json.dumps(checkpoint))

            staging = integration.Staging(path=Path(tmp) / "work" / "repo", base="main", added=["specs/user_story.md"])
            staging.path.mkdir(parents=True)
            prompts = []
            stack.enter_context(patch.dict(os.environ, {"DEVOPS_TARGET_OWNER": "acme", "CREWAI_INCLUDE_DATA": "false"}))
            stack.enter_context(patch.object(app, "OUTPUT_DIR", out))
            stack.enter_context(patch.object(app, "CHECKPOINT_FILE", out / ".checkpoint.json"))
            stack.enter_context(patch.object(app, "proj_tools", side_effect=AssertionError("no debe consultar MCP Projects")))
            stack.enter_context(patch("builtins.input", side_effect=lambda q="": prompts.append(q) or "s"))
            stack.enter_context(patch.object(integration, "stage_changes", return_value=staging))
            push = stack.enter_context(patch.object(integration, "commit_and_push"))
            run = stack.enter_context(patch.object(app, "run_step", return_value=DRAFT))
            stack.enter_context(patch.object(app, "hitl_approve", return_value=("s", "")))
            pr = stack.enter_context(patch.object(app, "create_pull_request", return_value={"number": 7, "url": "https://github.com/acme/ai-labs/pull/7"}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

            app.main()

            self.assertEqual(len(prompts), 1)
            self.assertIn("solo por el paso DevOps", prompts[0])
            self.assertIn("A specs/user_story.md", run.call_args.args[1].description)
            branch = push.call_args.args[1]
            self.assertTrue(branch.startswith("crewai/transfer-funds-"))
            pr.assert_called_once_with("feat: transfer funds", "## Resumen\nCuerpo", branch, "main")
            self.assertEqual(json.loads((out / ".checkpoint.json").read_text())["steps"]["devops"]["status"], "approved")
            self.assertIn("pull/7", (out / "specs/devops_report.md").read_text())
            self.assertEqual(integration.pending_changes(out), {"new": [], "modified": []})
            self.assertFalse(staging.path.exists())

    def test_rejected_devops_does_not_push_or_mark_integrated(self):
        steps = workflow_steps(False)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            out = Path(tmp) / "output"
            write(out, "specs/user_story.md", "story")
            checkpoint = {"workflow_mode": "standard", "feature_title": "F",
                          "steps": {step[0]: {"status": "approved", "output": "ok"} for step in steps}}
            write(out, ".checkpoint.json", json.dumps(checkpoint))
            staging = integration.Staging(path=Path(tmp) / "work" / "repo", base="main")
            staging.path.mkdir(parents=True)
            stack.enter_context(patch.dict(os.environ, {"DEVOPS_TARGET_OWNER": "acme", "CREWAI_INCLUDE_DATA": "false"}))
            stack.enter_context(patch.object(app, "OUTPUT_DIR", out))
            stack.enter_context(patch.object(app, "CHECKPOINT_FILE", out / ".checkpoint.json"))
            stack.enter_context(patch("builtins.input", return_value="s"))
            stack.enter_context(patch.object(integration, "stage_changes", return_value=staging))
            push = stack.enter_context(patch.object(integration, "commit_and_push"))
            stack.enter_context(patch.object(app, "run_step", return_value=DRAFT))
            stack.enter_context(patch.object(app, "hitl_approve", return_value=("n", "")))
            pr = stack.enter_context(patch.object(app, "create_pull_request"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

            app.main()

            push.assert_not_called()
            pr.assert_not_called()
            self.assertEqual(integration.pending_changes(out)["new"], ["specs/user_story.md"])


if __name__ == "__main__":
    unittest.main()
