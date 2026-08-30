# Third-Party Notices

Wish Builder integrates with the tools below, but does not include their packages in its wheel,
source archive, or Skill ZIP. Install each tool separately under its own license.

| Project | Pinned integration version | License | Source |
| --- | --- | --- | --- |
| Trellis CLI | `@mindfoldhq/trellis@0.6.15` | AGPL-3.0-only | https://github.com/mindfold-ai/Trellis |
| Trellis Core | `@mindfoldhq/trellis-core@0.6.15` | AGPL-3.0-only | https://github.com/mindfold-ai/Trellis |
| OpenAI Codex | `@openai/codex@0.149.0` | Apache-2.0 | https://github.com/openai/codex |
| Pi coding agent | `@earendil-works/pi-coding-agent@0.84.2` | MIT | https://www.npmjs.com/package/@earendil-works/pi-coding-agent |
| Oh My Pi coding agent | `@oh-my-pi/pi-coding-agent@17.4.0` | MIT | https://www.npmjs.com/package/@oh-my-pi/pi-coding-agent |

The versions above are compatibility or test baselines, not bundled dependencies and not proof
of backend dispatch qualification. The independently pinned backend version registry controls
exact-version admission. Current source qualifies only `Codex 0.149.0 / Windows`, at concurrency
one or two; the other listed backend/OS versions remain candidates.

Wish Builder itself is licensed under GPL-3.0-only. See [LICENSE](LICENSE).
