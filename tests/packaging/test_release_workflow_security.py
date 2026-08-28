from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> str:
    return (REPOSITORY_ROOT / ".github" / "workflows" / name).read_text(
        encoding="utf-8"
    )


class ReleaseWorkflowSecurityTests(unittest.TestCase):
    def _assert_trusted_revision_contract(self, workflow: str) -> None:
        verifier = workflow.split("  verify_candidate:", 1)[1].split(
            "\n  publish:", 1
        )[0]
        publisher = workflow.split("  publish:", 1)[1]
        verifier_checkout = verifier.split(
            "- name: Check out trusted verifier code at the workflow revision", 1
        )[1].split("- name: Verify immutable trusted workflow source ancestry", 1)[0]
        publisher_checkout = publisher.split(
            "- name: Check out the immutable trusted workflow revision", 1
        )[1].split("- name: Download bytes approved by the read-only verifier", 1)[0]

        self.assertIn("ref: ${{ github.sha }}", verifier_checkout)
        self.assertIn("ref: ${{ github.sha }}", publisher_checkout)
        self.assertNotIn("github.event.repository.default_branch", verifier_checkout)
        self.assertNotIn("github.event.repository.default_branch", publisher_checkout)
        self.assertEqual(
            2, workflow.count("TRUSTED_WORKFLOW_SHA: ${{ github.sha }}")
        )
        self.assertEqual(
            2,
            workflow.count(
                "WORKFLOW_DEFINITION_SHA: ${{ github.workflow_sha }}"
            ),
        )
        self.assertIn(
            "trusted_workflow_sha: "
            "${{ steps.trusted_source.outputs.trusted_workflow_sha }}",
            verifier,
        )
        self.assertIn(
            "VERIFIED_TRUSTED_WORKFLOW_SHA: "
            "${{ needs.verify_candidate.outputs.trusted_workflow_sha }}",
            publisher,
        )
        self.assertIn(
            '[[ "$VERIFIED_TRUSTED_WORKFLOW_SHA" == "$TRUSTED_WORKFLOW_SHA" ]]',
            publisher,
        )
        self.assertEqual(
            2,
            workflow.count(
                '[[ "$WORKFLOW_DEFINITION_SHA" == "$TRUSTED_WORKFLOW_SHA" ]]'
            ),
        )
        self.assertEqual(
            2,
            workflow.count('[[ "$TARGET_SHA" == "$TRUSTED_WORKFLOW_SHA" ]]'),
        )
        self.assertGreaterEqual(
            workflow.count(
                '[[ "$(jq -r .merge_base_commit.sha <<<"$comparison_json")" '
                '== "$TRUSTED_WORKFLOW_SHA" ]]'
            )
            + workflow.count(
                '[[ "$(jq -r .merge_base_commit.sha <<<"$trusted_comparison")" '
                '== "$TRUSTED_WORKFLOW_SHA" ]]'
            ),
            2,
        )
        self.assertIn("environment: release", publisher)

    def _assert_release_cleanup_contract(self, workflow: str) -> None:
        publisher = workflow.split("  publish:", 1)[1]
        cleanup = publisher.split("          cleanup_owned_release() {", 1)[1].split(
            "          trap cleanup_owned_release EXIT", 1
        )[0]
        self.assertIn("          draft_creation_attempted=1", publisher)
        attempted = publisher.index("          draft_creation_attempted=1")
        create = publisher.index('          gh release create "$tag"')
        exact_marker_check = (
            "'((.body // \"\") | split(\"\\n\")[0]) == $marker'"
        )

        self.assertLess(attempted, create)
        self.assertIn(
            '          draft_creation_attempted=1\n'
            '          gh release create "$tag"',
            publisher,
        )
        self.assertIn('"$draft_creation_attempted" -eq 1', cleanup)
        self.assertIn('"$published" -eq 0', cleanup)
        self.assertIn(
            "<!-- wish-builder-release-owner:"
            "$GITHUB_REPOSITORY:$GITHUB_RUN_ID:$GITHUB_RUN_ATTEMPT:$TARGET_SHA -->",
            publisher,
        )
        self.assertIn("--json databaseId,isDraft,tagName,targetCommitish,body", cleanup)
        self.assertIn('[[ "$release_id" =~ ^[1-9][0-9]*$ ]]', cleanup)
        self.assertIn('[[ "$release_id" == "$owned_release_id" ]]', cleanup)
        self.assertNotIn(
            '[[ "$(jq -r .isDraft <<<"$ownership_json")" == "true" ]]',
            cleanup,
        )
        self.assertIn(
            '[[ "$(jq -r .tagName <<<"$ownership_json")" == "$tag" ]]',
            cleanup,
        )
        self.assertIn(
            '[[ "$(jq -r .targetCommitish <<<"$ownership_json")" '
            '== "$TARGET_SHA" ]]',
            cleanup,
        )
        self.assertIn(exact_marker_check, cleanup)
        self.assertNotIn("contains($marker)", cleanup)
        self.assertIn(
            '"repos/$GITHUB_REPOSITORY/releases/$release_id"', cleanup
        )

    def test_promotable_ci_excludes_tag_pushes_and_requires_branch_refs(self) -> None:
        workflow = _workflow("ci.yml")
        trigger = workflow.split("\npermissions:", 1)[0]
        branch_ref_guard = (
            'if [[ "$EVENT_NAME" == "push" && ( "$PUSH_REF" != refs/heads/* '
            '|| "$PUSH_REF" != "refs/heads/$PUSH_REF_NAME" ) ]]; then'
        )

        self.assertIn('  push:\n    branches:\n      - "**"\n  pull_request:', trigger)
        self.assertNotIn("tags:", trigger)
        self.assertEqual(2, workflow.count("PUSH_REF: ${{ github.ref }}"))
        self.assertEqual(2, workflow.count(branch_ref_guard))
        self.assertEqual(
            2,
            workflow.count(
                'echo "Promotable push evidence requires an exact branch ref." >&2'
            ),
        )

    def test_read_only_verifier_accepts_only_the_same_repo_default_branch_run(
        self,
    ) -> None:
        workflow = _workflow("release.yml")
        verifier = workflow.split("  verify_candidate:", 1)[1].split(
            "\n  publish:", 1
        )[0]

        self.assertIn("permissions:\n      actions: read\n      contents: read", verifier)
        self.assertNotIn("contents: write", verifier)
        self.assertIn(
            '[[ "$WORKFLOW_REF" == "refs/heads/$DEFAULT_BRANCH" ]]',
            verifier,
        )
        self.assertIn(
            '[[ "$TARGET_SHA" == "$TRUSTED_WORKFLOW_SHA" ]]',
            verifier,
        )
        for assertion in (
            '[[ "$(jq -r .repository.full_name <<<"$run_json")" == "$GITHUB_REPOSITORY" ]]',
            '[[ "$(jq -r .head_repository.full_name <<<"$run_json")" == "$GITHUB_REPOSITORY" ]]',
            '[[ "$(jq -r .head_branch <<<"$run_json")" == "$DEFAULT_BRANCH" ]]',
            '[[ "$(jq -r .head_sha <<<"$run_json")" == "$TARGET_SHA" ]]',
            '[[ "$(jq -r .status <<<"$run_json")" == "completed" ]]',
            '[[ "$(jq -r .conclusion <<<"$run_json")" == "success" ]]',
            '[[ "$(jq -r .event <<<"$run_json")" == "push" ]]',
            '[[ "$(jq -r .path <<<"$run_json")" == ".github/workflows/ci.yml" ]]',
        ):
            with self.subTest(assertion=assertion):
                self.assertIn(assertion, verifier)

    def test_write_token_never_executes_candidate_controlled_python(self) -> None:
        workflow = _workflow("release.yml")
        verifier = workflow.split("  verify_candidate:", 1)[1].split(
            "\n  publish:", 1
        )[0]
        publisher = workflow.split("  publish:", 1)[1]

        self.assertIn("python scripts/ci_release.py", verifier)
        self.assertIn(
            "Check out trusted verifier code at the workflow revision", verifier
        )
        self.assertIn("ref: ${{ github.sha }}", verifier)
        self.assertIn("persist-credentials: false", verifier)
        self.assertNotIn("candidate-source", verifier)
        self.assertIn("--repository-root .", verifier)
        self.assertIn(
            "python scripts/build_distributions.py --outdir dist-rebuilt",
            verifier,
        )
        self.assertIn("--require-hashes --only-binary=:all:", verifier)
        self.assertIn("-r requirements/build.txt", verifier)
        self.assertIn("cmp --silent", verifier)
        self.assertIn("contents: write", publisher)
        self.assertIn("ref: ${{ github.sha }}", publisher)
        self.assertIn("environment: release", publisher)
        self.assertIn("name: verified-release-assets", publisher)
        self.assertNotIn("setup-python", publisher)
        self.assertNotIn("python ", publisher.lower())
        self.assertNotIn("scripts/", publisher)
        self.assertNotIn("ref: ${{ inputs.target_sha }}", publisher)
        self.assertIn(
            '"repos/$GITHUB_REPOSITORY/compare/$TARGET_SHA...$default_tip"',
            publisher,
        )
        self._assert_trusted_revision_contract(workflow)

    def test_read_only_verifier_requires_independent_release_approval(self) -> None:
        verifier = _workflow("release.yml").split(
            "  verify_candidate:", 1
        )[1].split("\n  publish:", 1)[0]
        policy = verifier.split(
            "- name: Require protected release approval policy", 1
        )[1].split(
            "- name: Check out trusted verifier code at the workflow revision", 1
        )[0]

        self.assertIn(
            'gh api "repos/$GITHUB_REPOSITORY/environments/release"', policy
        )
        self.assertIn('.type == "required_reviewers"', policy)
        self.assertIn("(.reviewers | type == \"array\" and length >= 1)", policy)
        self.assertIn(".prevent_self_review == true", policy)
        self.assertIn(
            ".deployment_branch_policy.protected_branches == true", policy
        )
        self.assertIn(
            ".deployment_branch_policy.custom_branch_policies == false", policy
        )

    def test_tag_creation_is_exact_non_force_and_transport_aware(self) -> None:
        publisher = _workflow("release.yml").split("  publish:", 1)[1]
        create_tag = publisher.split("            2)", 1)[1].split(
            "              remote_tag_lines=", 1
        )[0]

        self.assertIn("git ls-remote --exit-code --tags origin", publisher)
        self.assertIn('case "$tag_lookup_status" in', publisher)
        self.assertIn("            0)", publisher)
        self.assertIn("            2)", publisher)
        self.assertIn("            *)", publisher)
        self.assertIn("remote lookup failed", publisher)
        self.assertIn('tag --annotate "$tag" "$TARGET_SHA"', create_tag)
        self.assertIn(
            'git push origin "refs/tags/$tag:refs/tags/$tag"', create_tag
        )
        self.assertNotIn("--force", create_tag)
        self.assertIn('peeled_ref="refs/tags/$tag^{}"', publisher)
        self.assertIn('[[ "$peeled_sha" == "$TARGET_SHA" ]]', publisher)
        self.assertIn(
            '[[ "$prepublish_peeled_sha" == "$TARGET_SHA" ]]', publisher
        )
        self.assertIn('[[ "$final_peeled_sha" == "$TARGET_SHA" ]]', publisher)
        self.assertIn("--verify-tag", publisher)

    def test_exact_existing_tag_is_rerunnable_and_failed_draft_leaves_it(self) -> None:
        publisher = _workflow("release.yml").split("  publish:", 1)[1]
        existing_tag_case = publisher.split("            0)", 1)[1].split(
            "            2)", 1
        )[0]
        cleanup = publisher.split("          cleanup_owned_release() {", 1)[1].split(
            "          trap cleanup_owned_release EXIT", 1
        )[0]

        self.assertIn("already exists", existing_tag_case)
        self.assertNotIn("exit 1", existing_tag_case)
        self.assertIn('[[ "$peeled_sha" == "$TARGET_SHA" ]]', publisher)
        self.assertIn("A release for $tag already exists", publisher)
        self.assertNotIn("refs/tags/", cleanup)
        self.assertNotIn("git push", cleanup)
        self.assertNotIn("--cleanup-tag", cleanup)
        self.assertLess(
            publisher.index("draft_creation_attempted=1"),
            publisher.index('gh release create "$tag"'),
        )
        self.assertIn('gh release upload "$tag" release-assets/*', publisher)

    def test_failed_run_cleans_up_only_its_own_release(self) -> None:
        workflow = _workflow("release.yml")
        publisher = workflow.split("  publish:", 1)[1]
        cleanup = publisher.split("          cleanup_owned_release() {", 1)[1].split(
            "          trap cleanup_owned_release EXIT", 1
        )[0]

        self.assertIn(
            'run_marker="<!-- wish-builder-release-owner:$GITHUB_REPOSITORY:'
            '$GITHUB_RUN_ID:$GITHUB_RUN_ATTEMPT:$TARGET_SHA -->"',
            publisher,
        )
        self.assertIn('"$draft_creation_attempted" -eq 1', cleanup)
        self.assertIn('"$published" -eq 0', cleanup)
        self.assertIn(
            '--json databaseId,isDraft,tagName,targetCommitish,body', cleanup
        )
        self.assertIn('[[ "$release_id" =~ ^[1-9][0-9]*$ ]]', cleanup)
        self.assertIn('[[ "$release_id" == "$owned_release_id" ]]', cleanup)
        self.assertNotIn(
            '[[ "$(jq -r .isDraft <<<"$ownership_json")" == "true" ]]',
            cleanup,
        )
        self.assertIn(
            "'((.body // \"\") | split(\"\\n\")[0]) == $marker'", cleanup
        )
        self.assertNotIn("contains($marker)", cleanup)
        self.assertIn(
            '"repos/$GITHUB_REPOSITORY/releases/$release_id"', cleanup
        )
        self.assertNotIn("--cleanup-tag", cleanup)
        self._assert_release_cleanup_contract(workflow)

    def test_post_publish_failure_remains_cleanup_eligible(self) -> None:
        publisher = _workflow("release.yml").split("  publish:", 1)[1]
        publish = publisher.index('          gh release edit "$tag"')
        final_checks = publisher.index("          final_tag_lines=", publish)
        marked_published = publisher.index("          published=1", final_checks)
        cleanup = publisher.split("          cleanup_owned_release() {", 1)[1].split(
            "          trap cleanup_owned_release EXIT", 1
        )[0]

        self.assertLess(publish, final_checks)
        self.assertLess(final_checks, marked_published)
        self.assertIn('"$published" -eq 0', cleanup)
        self.assertNotIn(
            '[[ "$(jq -r .isDraft <<<"$ownership_json")" == "true" ]]',
            cleanup,
        )

    def test_existing_release_is_rejected_before_any_tag_mutation(self) -> None:
        publisher = _workflow("release.yml").split("  publish:", 1)[1]
        first_release_check = publisher.index("          existing_release_ids=")
        tag_lookup = publisher.index("          remote_tag_lines=", first_release_check)
        tag_create = publisher.index("                tag --annotate", tag_lookup)
        tag_push = publisher.index("              git push origin", tag_create)
        second_release_check = publisher.index(
            "          existing_release_ids=", first_release_check + 1
        )

        self.assertLess(first_release_check, tag_lookup)
        self.assertLess(first_release_check, tag_create)
        self.assertLess(first_release_check, tag_push)
        self.assertLess(tag_push, second_release_check)
        self.assertEqual(2, publisher.count("          existing_release_ids="))

    def test_verified_assets_survive_environment_approval_wait(self) -> None:
        workflow = _workflow("release.yml")
        verifier = workflow.split("  verify_candidate:", 1)[1].split(
            "\n  publish:", 1
        )[0]
        upload = verifier.split(
            "      - name: Upload the read-only verifier result", 1
        )[1]

        self.assertIn("          retention-days: 14", upload)

    def test_trusted_revision_reverse_cases_fail_the_static_contract(self) -> None:
        workflow = _workflow("release.yml")
        unsafe_cases = {
            "moving verifier ref": workflow.replace(
                "ref: ${{ github.sha }}",
                "ref: ${{ github.event.repository.default_branch }}",
                1,
            ),
            "moving publisher ref": workflow.rsplit(
                "ref: ${{ github.sha }}", 1
            )[0]
            + "ref: ${{ github.event.repository.default_branch }}"
            + workflow.rsplit("ref: ${{ github.sha }}", 1)[1],
            "unbound verifier output": workflow.replace(
                "${{ needs.verify_candidate.outputs.trusted_workflow_sha }}",
                "${{ inputs.target_sha }}",
                1,
            ),
            "missing environment": workflow.replace("    environment: release\n", "", 1),
        }

        for case, unsafe_workflow in unsafe_cases.items():
            with self.subTest(case=case):
                with self.assertRaises(AssertionError):
                    self._assert_trusted_revision_contract(unsafe_workflow)

    def test_release_cleanup_reverse_cases_fail_the_static_contract(self) -> None:
        workflow = _workflow("release.yml")
        attempted_after_create = workflow.replace(
            "          draft_creation_attempted=1\n", "", 1
        ).replace(
            "            --draft\n\n          ownership_json=",
            "            --draft\n"
            "          draft_creation_attempted=1\n\n"
            "          ownership_json=",
            1,
        )
        unsafe_cases = {
            "no attempted state": workflow.replace(
                "          draft_creation_attempted=1\n", "", 1
            ),
            "attempted state after create": attempted_after_create,
            "non-unique run marker": workflow.replace(
                "<!-- wish-builder-release-owner:$GITHUB_REPOSITORY:"
                "$GITHUB_RUN_ID:$GITHUB_RUN_ATTEMPT:$TARGET_SHA -->",
                "wish-builder-release",
                1,
            ),
            "non-exact marker": workflow.replace(
                "'((.body // \"\") | split(\"\\n\")[0]) == $marker'",
                "'(.body // \"\") | contains($marker)'",
                1,
            ),
            "no captured release id check": workflow.replace(
                '&& { [[ -z "$owned_release_id" ]] || [[ "$release_id" == "$owned_release_id" ]]; }',
                "&& true",
                1,
            ),
            "no tag check": workflow.replace(
                '&& [[ "$(jq -r .tagName <<<"$ownership_json")" == "$tag" ]]',
                "&& true",
                1,
            ),
            "no target check": workflow.replace(
                '&& [[ "$(jq -r .targetCommitish <<<"$ownership_json")" '
                '== "$TARGET_SHA" ]]',
                "&& true",
                1,
            ),
        }

        for case, unsafe_workflow in unsafe_cases.items():
            with self.subTest(case=case):
                with self.assertRaises(AssertionError):
                    self._assert_release_cleanup_contract(unsafe_workflow)

    def test_release_uses_only_the_ephemeral_github_token(self) -> None:
        workflow = _workflow("release.yml")

        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("personal_access_token", workflow.lower())
        self.assertNotIn("GH_PAT", workflow)


if __name__ == "__main__":
    unittest.main()
