# Review-and-edit before the final render

A plan for an optional review step between "generate the parts" and "render the
video": the user sees every scene's image/clip, narration line and subtitle,
replaces or regenerates anything that came out wrong, and only then renders.

The existing one-click path must stay byte-identical. The review step is opt-in.

## What already exists (do not rebuild)

| Thing | Where | Use it for |
| --- | --- | --- |
| Stage-wise pipeline | `app/services/task.py:1573` `_run_pipeline(stop_at=...)` | `stop_at` already stops cleanly after `script`, `terms`, `audio`, `subtitle`, `materials` |
| Per-task manifest | `app/services/task_artifacts.py` -> `storage/tasks/<id>/script.json` | atomic + locked writes, and `patch_script_data()` for partial updates |
| Single-keyword image generation | `app/services/material.py:1807` `generate_images_openai(search_term, minimum_duration, video_aspect, save_dir)` | regenerating exactly one scene -- already the right granularity |
| Upload validation | `app/services/material_upload.py:296` `save_material_upload()` | "replace this scene with my own file" |
| Media streaming | `app/controllers/v1/video.py:517` `GET /stream/{file_path:path}` | serving thumbnails/clips/audio to the browser |
| Busy detection | `app/services/task.py:122` `is_task_busy()` | refusing edits while a render is running |

### `video_project.py` is not the render engine

`app/services/video_project.py` (945 lines, tested, `docs/video-projects.md`) is a
content-addressed store with per-scene revisions, fingerprint caching and
`plan`/`status`/`cancel`/`compare`. It is tempting as the editor's backend and it
is the wrong choice:

* it has **zero** subtitle, caption, transition or concat-mode support --
  `grep -ci subtitle app/services/video_project.py` returns 0;
* `RenderSettings` accepts only `width`, `height`, `fps`, `narration_volume`,
  `bgm_volume`, and `create_revision()` rejects every unknown field;
* it requires **per-scene prepared audio** ("narration requires prepared
  audio"), while the pipeline makes one continuous narration track.

Routing the editor through it would silently drop subtitle styling, the spring
animation, transitions and concat modes -- everything in `app/services/video.py`.

So: **the renderer stays `generate_final_videos()`.** `video_project` keeps its
CLI life untouched. What we borrow from it is the *idea* of a per-scene manifest,
which we add to the `script.json` the task already writes.

## Architecture

The review step is a pause, not a new pipeline:

```
            stop_at="materials"                     start_from="video"
 params ---------------------------> [review UI] --------------------------> mp4
         script terms audio subtitle    edit        generate_final_videos
                materials             regenerate
```

One addition carries the whole feature: **`start_from`**, the mirror of the
`stop_at` that already exists. Everything `generate_final_videos()` needs is
persistable -- `audio_file`, `audio_duration`, `subtitle_path`, the material list
and `params` -- so resuming needs no in-memory state. (`sub_maker` is not needed
on resume: the subtitle stage has already written the `.srt`.)

## Steps

### 1. `start_from` resume (`app/services/task.py`)

Add `start_from: str = "script"` to `_run_pipeline()` and `start()`. Each stage
becomes "skip and load from the manifest if it falls before `start_from`".

* Stage order: `script`, `terms`, `audio`, `subtitle`, `materials`, `video`.
* Default `"script"` reproduces today's behaviour exactly -- no new branch taken.
* Validate `start_from` against the stage list in the preflight block and fail
  with a named error. **No silent fallback**: if `start_from="video"` and the
  manifest has no audio, that is a clear error, not a quiet regeneration.
* Persist at the end of the materials stage what resume needs. The
  `stop_at="materials"` branch (task.py:1931) already writes `materials` into
  task state; extend it with `audio_file`, `audio_duration` and `subtitle_path`.

Smallest honest test: run `stop_at="materials"`, then `start_from="video"`, and
assert the result matches a single uninterrupted run.

### 2. The scene manifest (`app/services/task_artifacts.py`)

At the end of the materials stage, patch `script.json` with a `scenes` array:

```json
{"scenes": [
  {"index": 0, "keyword": "a farmer planting seeds",
   "narration": "Every harvest starts with one seed.",
   "material": "storage/tasks/<id>/1.png", "kind": "image",
   "source": "openai_image", "duration": 5.0, "locked": false}
]}
```

* `narration` comes from `utils.split_string_by_punctuations()`
  (`app/utils/utils.py:306`), the same splitter the subtitle stage uses, so the
  lines match the captions the viewer will see.
* Keyword order is already script order when `match_materials_to_script` is on
  (see the comment at task.py:443) -- use it for keyword/line pairing, and fall
  back to positional pairing when it is off.
* `locked` marks a scene the user approved or replaced by hand, so a later bulk
  regeneration leaves it alone.
* `kind` matters: `openai_image`/`codex_image` yield stills, the stock and
  video-model sources yield clips. The UI and the render path both branch on it.

Use `patch_script_data()`, never a bare write -- it holds the task lock.

### 3. Editing endpoints (new `app/controllers/v1/editor.py`)

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/v1/tasks/{id}/manifest` | the editor's read model: scenes + media URLs |
| POST | `/api/v1/tasks/{id}/scenes/{i}/material` | regenerate this scene only; optional `keyword` and `image_style` override |
| PUT | `/api/v1/tasks/{id}/scenes/{i}` | replace by upload, edit narration, lock/unlock |
| DELETE | `/api/v1/tasks/{id}/scenes/{i}` | drop a scene |
| POST | `/api/v1/tasks/{id}/scenes/reorder` | new index order |
| POST | `/api/v1/tasks/{id}/audio` | re-run TTS after narration edits (whole track) |
| POST | `/api/v1/tasks/{id}/render` | resume with `start_from="video"` |

Rules:

* Every one of these refuses with 409 when `is_task_busy()` is true.
* Scene regeneration calls the existing per-term functions
  (`generate_images_openai`, `material.download_videos`) -- it must not re-enter
  `get_video_materials()`, which plans the whole set at once.
* Editing narration invalidates audio **and** subtitles. Return that fact to the
  caller and record it in the manifest (`audio_stale: true`); do not silently
  re-run TTS, and do not allow a render with stale audio.
* Paths from the client are never trusted: resolve them inside the task
  directory with the existing `app/utils/file_security.py` helpers.

### 4. The review UI, phase 1 (new `webui/review.py`)

Streamlit cannot do a drag-and-drop timeline, and it does not need to for this.
A vertical scene list delivers what matters: see, replace, regenerate, render.

* `webui/Main.py` is 9586 lines. Put the panel in `webui/review.py` and call it
  from one place in Main.py.
* A `Review scenes before rendering` checkbox in Video Settings. Unchecked, the
  Generate button behaves exactly as it does today.
* Checked, Generate runs `stop_at="materials"`, then renders one row per scene:
  thumbnail (or `st.video` for clips), narration text area, keyword field,
  `Regenerate` / `Replace...` / `Lock` buttons -- then a single `Render video`.
* Wrap each row in `st.fragment` (Streamlit 1.66.1 supports it) so touching one
  scene does not rerun the whole 9.5k-line script and re-mount every player.
* i18n: add each new key to all 14 files in `webui/i18n/` -- `test_webui_i18n.py`
  requires every key in every locale even though `tr()` falls back to English.

### 5. Phase 2, optional: a real timeline

Only if frame-level control is still wanted once phase 1 is in use.

* A small SPA served by the existing FastAPI app, opened from a WebUI button;
  `GET /stream/{file_path:path}` already feeds it.
* Preview by playing the per-scene media back to back -- no in-browser
  compositor, no WebCodecs.
* `wavesurfer.js` for the narration waveform, `SortableJS` or `dnd-kit` for
  reordering. Skip `react-timeline-editor` (unmaintained); skip Remotion (it
  would replace the renderer, and the licence does not suit this).
* Avoid a custom Streamlit component (`declare_component`): it drags a Node
  build into the repo and round-trips state badly on every drag.

## Constraints this plan is held to

* Add, never remove. Every provider keeps working; `start_from` defaults to the
  current behaviour; `video_project.py` is not touched.
* No change to an existing `config.toml`; the new checkbox defaults to off.
* No silent fallback between providers, or between fresh and cached artifacts.
* Existing tests pass unchanged.

## Order of work

1. `start_from` + resume test. Nothing user-visible, and it is the hard part.
2. Scene manifest + its test.
3. Endpoints + tests.
4. Phase 1 UI + i18n.

Steps 1 and 2 are independently useful: they make a failed render resumable
instead of a full re-run.

---

# As built

Steps 1–4 are implemented. Step 5 (the timeline SPA) is deliberately not: the
plan gates it on phase 1 being in use first.

## Where things ended up

| Piece | File |
| --- | --- |
| `start_from` resume | `app/services/task.py` (`_run_pipeline`, `start`, `_resumed_audio`) |
| Scene list, stage order, path boundary | `app/services/scene_manifest.py` (new) |
| Scene editing operations | `app/services/scene_editor.py` (new) |
| Manifest reader | `app/services/task_artifacts.py` (`read_script_data`) |
| HTTP routes | `app/controllers/v1/editor.py` (new), registered in `app/router.py` |
| Request/response models | `app/models/schema.py` (appended) |
| Review panel | `webui/review.py` (new), hooked into `webui/Main.py` |
| Stage plumbing for the WebUI | `app/services/webui_task.py` |
| Translations | all 14 files in `webui/i18n/` (+25 keys each) |

Tests: `test_scene_manifest.py` (30), `test_scene_editor.py` (32),
`test_pipeline_resume.py` (15), `test_controller_editor.py` (13),
`test_webui_review_i18n.py` (3).

## Deviations from the plan above

* **`GET /tasks/{id}/scenes`, not `/manifest`.** The response is the scene list,
  so it is named for what it returns.
* **No per-scene `image_style` override.** The style is a prompt template read
  from config inside `material._openai_image_prompt`, so a per-request override
  would have to be threaded through `download_videos` into every source. The
  keyword override is implemented, which is what actually changes the picture;
  the style remains a global setting in Video Settings that applies to the next
  regeneration.
* **Regeneration goes through `material.download_videos`, not
  `generate_images_openai`.** Narrowing `search_terms` to one keyword and the
  duration to one clip reuses the single path that already reconciles every
  source, instead of special-casing one of them.
* **The `stop_at="materials"` return value and task state were left alone.**
  Widening them broke `test_jalapeno.py`, which pins that dict exactly.
  Everything a resume needs is in `script.json`, which is the copy that
  survives a WebUI restart anyway.
* **Scene-review translations are real, in all 14 locales.** Adding keys to
  `en.json` makes `test_webui_i18n.py` require them in all 12 secondary
  locales, and the alternative was editing that test's `ENGLISH_FALLBACK_KEYS`.

## Known limits, measured not guessed

* **Stock sources return the same file on regeneration.** Pexels, Pixabay and
  Coverr searches are cached by `_search_videos_with_cache`, so regenerating a
  scene with an unchanged keyword very likely gives back the same clip. Change
  the keyword to get something different. Locally generated images sample fresh
  every call, so regeneration there does produce a new picture. The cache is
  not bypassed on purpose: that would spend somebody's third-party quota
  without asking.
* **`start_from="subtitle"` only does anything with Whisper.** `generate_subtitle`
  needs a `sub_maker` timeline from TTS for the edge and Azure providers, and a
  resume has no such object. It warns and returns no subtitles rather than
  inventing a timeline — the same existing behaviour as a custom audio file.
  Re-recording narration therefore runs `start_from="audio"`, which rebuilds
  the timeline properly.
* **Editing narration invalidates audio and subtitles together.** The manifest
  records `audio_stale`, the render refuses while it is set, and the panel
  disables the render button and offers Re-record instead. Nothing re-runs TTS
  on its own, because that would both cost a synthesis and silently void every
  subtitle timing.
* **Ground-level prop loss and the eyebrow-frown risk** in the stickman preset
  are unchanged and unrelated; see `app/models/image_style.py`.
* **`locked` is a marker, not yet an enforcement.** The plan introduced it so a
  future bulk regeneration would skip approved scenes. Regenerating or
  replacing a scene sets it, and the panel can toggle it, but no bulk operation
  exists yet, so nothing currently reads it to skip work. It is honest state
  about what the user has approved; it is not protection against anything.
* **`st.rerun(scope="fragment")` is deliberately not used after an edit.** A
  fragment re-runs with the arguments it was first called with, so a row would
  redisplay the pre-edit scene. Edits rerun the page; only typing stays local
  to the row, which is where the cost actually was.
