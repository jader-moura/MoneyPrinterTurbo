# Reviewing scenes before the render

Optional. Unticked, nothing changes: one click still goes from a subject to a
finished video, with every provider behaving exactly as before.

## In the WebUI

1. In **Video Settings**, tick **Review scenes before rendering**.
2. Click Generate as usual. The run writes the script, narrates it, builds the
   subtitles and fetches or generates the pictures — then stops.
3. The review panel appears in place of the finished video, one row per scene:
   the picture or clip, its keyword and its narration line.
4. Fix whatever came out wrong:
   * **Regenerate** — a new picture for that scene alone. Change the keyword
     first if the subject itself was wrong; that is usually what fixes it.
   * **Replace with your own file** — upload an image or clip. It is copied
     into the task directory, so the task stays self-contained.
   * **Move up / Move down**, **Remove** — reorder or drop a scene.
   * **Lock** — mark a scene you are happy with.
   * Edit the **Narration** text to change what is said over that scene.
5. Click **Render video**.

Editing narration invalidates the audio *and* the subtitles, because the
timings no longer line up. The panel says so, disables Render and offers
**Re-record narration**, which re-synthesises the voice from the edited lines
and rebuilds the subtitles. Nothing re-runs TTS on its own: that would spend a
synthesis and void every subtitle timing without being asked.

A reviewed run is still there after a browser refresh or a WebUI restart — the
scene list lives in the task's `script.json`, not in session state.

## The timeline editor

The CapCut-style timeline — preview player, a filmstrip you drag clips around
in, trim handles — appears **in the review panel itself**, already loaded with
that task, as soon as a review run finishes. There is also a link to open it
full-screen in its own tab, at `http://127.0.0.1:8080/editor/?task=<task id>`.

It is a page served by the API process rather than a Streamlit widget, because
Streamlit reruns the whole script on every interaction and so cannot do
dragging or a playhead that stays in sync with the picture. It is embedded as
an iframe, which keeps its own origin, so its API calls and media requests
need no CORS and nothing has to be handed to it from Streamlit.

**It needs the API server running.** `scripts/start-local.sh` now starts it
alongside everything else. Running the WebUI on its own is still fine — the
one-click path never touches the API — but the panel will say the timeline is
unavailable and tell you how to start it. By hand that is:

```bash
.venv/Scripts/python.exe main.py     # listens on config.toml's listen_port, 8080 by default
```

There you can:

* **Drag a clip** along the video track to reorder it.
* **Drag either edge** to trim: the left edge moves the in-point, the right
  edge changes how long the clip holds.
* **Set the numbers directly** in the inspector (in-point, duration), or press
  **Auto** to hand that scene's length back to the pipeline.
* **Regenerate, lock or delete** the selected scene, same as in the panel.
* **Scrub** by clicking the ruler, and **Play** to preview the cut with the
  narration over it.
* **Write the YouTube title, description and thumbnail** in the **YouTube**
  panel, next to Script, Effects, Subtitles and Sound. It writes them for the
  shape this video actually is and draws the thumbnail at that shape.

The header keeps a running total against the narration length and warns when
they drift apart.

**Timing only takes effect once you set it.** Until a clip is trimmed, the
renderer decides lengths exactly as it always has, from the narration duration
and the clip-duration setting. The moment any scene is trimmed, the whole task
renders from the explicit layout instead — otherwise the clips you had not
touched would keep their automatic lengths and the cut would not match what you
laid out. Pressing **Auto** on every scene returns the task to the old
behaviour.

## Over the API

Run the pipeline to the materials stage, then edit and render:

```bash
# 1. Generate everything except the video. Same body as POST /videos.
curl -X POST localhost:8080/api/v1/materials \
  -H 'Content-Type: application/json' \
  -d '{"video_subject":"saving money","video_source":"openai_image"}'
# -> {"data": {"task_id": "..."}}; poll GET /api/v1/tasks/$TASK until complete

# 2. See the scenes
curl localhost:8080/api/v1/tasks/$TASK/scenes

# 3. Regenerate scene 0 with a better keyword
curl -X POST localhost:8080/api/v1/tasks/$TASK/scenes/0/material \
  -H 'Content-Type: application/json' -d '{"keyword":"a hand dropping a coin into a jar"}'

# 4. Reorder, then render
curl -X POST localhost:8080/api/v1/tasks/$TASK/scenes/reorder \
  -H 'Content-Type: application/json' -d '{"order":[1,0,2]}'
curl -X POST localhost:8080/api/v1/tasks/$TASK/render
```

| Route | Does |
| --- | --- |
| `POST /api/v1/materials` | start a run that stops before the render |
| `GET /api/v1/tasks/{id}/scenes` | the scene list, with playable URLs |
| `POST /api/v1/tasks/{id}/scenes/{i}/material` | regenerate that scene; optional `keyword` |
| `POST /api/v1/tasks/{id}/scenes/{i}/replace` | use an uploaded file |
| `PUT /api/v1/tasks/{id}/scenes/{i}` | edit `narration`, `keyword` or `locked` |
| `DELETE /api/v1/tasks/{id}/scenes/{i}` | drop a scene |
| `POST /api/v1/tasks/{id}/scenes/reorder` | `order` must be a full permutation |
| `PUT /api/v1/tasks/{id}/script` | rewrite the whole narration script |
| `PUT /api/v1/tasks/{id}/scenes/{i}/effects` | `transition`, `image_motion`, `cut_sound` for one scene |
| `PUT /api/v1/tasks/{id}/subtitles/{i}` | fix one caption's words, leaving the voice alone |
| `GET`/`PUT /api/v1/tasks/{id}/subtitles/style` | caption font, size, colour, outline, box, position |
| `GET`/`PUT /api/v1/tasks/{id}/effects` | the transition for the whole video; `apply_to_scenes` clears per-scene choices |
| `GET`/`PUT /api/v1/tasks/{id}/cut-sound` | the sound at every cut, or `""` to remove it |
| `GET`/`PUT /api/v1/tasks/{id}/publish` | the YouTube title, description, tags and thumbnail brief |
| `POST /api/v1/tasks/{id}/publish/text` | write them from the subject and script, for this video's aspect |
| `POST /api/v1/tasks/{id}/publish/thumbnail/scene` | use scene `index`'s own picture, cropped to this video's aspect |
| `POST /api/v1/tasks/{id}/publish/thumbnail` | draw the thumbnail at this video's aspect; optional `prompt` |
| `PUT /api/v1/tasks/{id}/publish/thumbnail/text` | the words burnt onto the thumbnail, and how they look |
| `GET /api/v1/tasks/{id}/publish/thumbnail` | download the thumbnail file |
| `GET /api/v1/library` | every task on disk, newest first, paged |
| `GET /api/v1/cut-sound/{name}` | play one transition sound in the editor |
| `GET /api/v1/fonts/{name}` | serve one subtitle font to the editor's preview |
| `GET`/`PUT /api/v1/tasks/{id}/music` | the background music of this video and its volume |
| `GET /api/v1/music/{name}` | play one track from the music library |
| `POST /api/v1/tasks/{id}/audio` | re-record narration and rebuild subtitles |
| `POST /api/v1/tasks/{id}/render` | render the reviewed scenes |
| `GET /api/v1/tasks/{id}/timeline` | the scene list plus each material's real length |
| `PUT /api/v1/tasks/{id}/scenes/{i}/timing` | set `trim_start` / `duration`, or `reset` |

Status codes: `409` means the task is still running and the edit would race the
render — wait and retry. `400` means the request itself needs changing, and the
message says what. Nothing falls back to another provider silently.

## What to expect

* **Regenerating a stock-footage scene usually returns the same clip.** Pexels,
  Pixabay and Coverr searches are cached, so an unchanged keyword gives back
  what it gave before. Change the keyword. Locally generated images sample
  afresh every call, so regeneration there really does produce a new picture.
* **Scene timings are not editable here.** The renderer computes them from the
  narration length, the concat mode and the transitions. The review step is
  about *what is in* each scene, not how long it lasts.
* **Reordering moves the picture, not the words.** The narration is one take
  and the subtitles are timed against it, so what is heard at a given second
  does not change when a picture moves to another slot. Each scene shows the
  lines spoken while it is on screen, recomputed from the subtitle timing on
  every read — move a picture, hold it longer or delete it and the lines
  redistribute to match. Re-recording uses the script in its own order, so it
  can no longer be shuffled by a reorder.
* **To change the words, edit the script.** `PUT /tasks/{id}/script` (or
  **Script** in the editor) rewrites the whole narration, which is where
  sentence order, splits and rewrites belong. Editing one scene's text still
  works and overrides just that stretch; clearing it drops those lines from the
  next recording. Either way the recorded voice goes stale and you choose when
  to re-record.
* **Each scene can have its own look and sound on the cut.** `transition`
  (fade, slide, zoom, shuffle, or `none` for a hard cut), `image_motion`
  (`zoom` or `static`) and `cut_sound` (a file from the sound library, or
  `none` for silence). Leave any of them empty and that scene follows the
  video-wide setting; `none` is a choice and overrides it. Picking a
  transition or a cut sound puts the task on the edit-list render path, the
  same one dragging the timeline uses.
* **Changing the motion re-renders that clip.** Motion is baked in when the
  picture is made, not applied at render, so the clip is rebuilt from the
  source PNG in the task folder — a few seconds, and instant once that
  combination has been rendered before. Stock footage has no source image, so
  it cannot change motion and says so.
* **Captions and the script are different things.** `PUT .../script` changes
  what is *spoken*; it needs a re-record, which regenerates the captions from
  the new audio. `PUT .../subtitles/{i}` changes only the words on screen,
  leaving the voice and every timing untouched — that is the one to use when
  the transcription misheard a word. A re-record afterwards discards
  caption-only edits, because the captions are rebuilt from the new voice.
* **The editor opens on your videos.** Without a `?task=` it lists every task
  that has a manifest on disk, newest first, with a frame, the subject, the
  scene count and whether it has been rendered — click one to open it and
  render again. It reads the disk rather than `GET /tasks`, which only holds
  what the running process has seen and is empty after a restart.
* **The caption's look is a video-wide setting.** Font, size, colour,
  outline, the box behind the text and the vertical position all live in
  `.../subtitles/style`, and the editor previews them on the picture in the
  real font at the real size. Colours are `#RRGGBB`; an unknown font or
  position is refused when it is chosen, not when the render reaches the last
  step.
* **The transition can be changed for the whole video at once.** `PUT
  .../effects` sets the one used wherever a scene has not chosen its own.
  Scenes that did choose keep their choice unless `apply_to_scenes` is set,
  which clears them — without that, changing the video-wide transition leaves
  those scenes untouched and looks like the change did not save.
* **The cut sound can be taken off entirely.** `PUT .../cut-sound` with an
  empty `sound` removes it from the whole video; a scene's own `cut_sound`
  only ever silences its own cut. The volume stays video-wide.
* **Background music belongs to the video, not to a scene.** `PUT
  .../music` with a track name, `random`, or an empty string for none, plus a
  volume between 0 and 1. It writes this task's own parameters, so other
  videos are unaffected. A video scored by an external provider (Sonilo,
  ElevenLabs) reports `external` and is left alone.
* **The thumbnail is normally one of the video's own pictures.** The panel
  shows every scene's picture as a row of tiles; clicking one crops it to the
  thumbnail shape and is done in about a second. It needs no image service, it
  cannot fail the way a fresh generation can, and it cannot produce a picture
  that does not match the video — which is the usual complaint about a
  generated thumbnail. Drawing a new one is still there, folded away under
  *Or draw a new picture instead*, for a picture the video does not contain.
  The panel says which scene the current thumbnail came from, and the tile it
  came from is outlined. Scene materials are usually mp4 (the picture is baked
  into a motion clip when it is drawn), so the frame at t=0 is taken — a zoom
  starts at 1.0, so the first frame is the fullest view of that picture.
* **Words can be burnt onto the thumbnail.** The text defaults to the title
  and the font to this video's own caption font, so it matches the video
  without choosing anything. Position, size, colour, outline and an optional
  dark band are adjustable; size and outline are percentages (of the picture's
  height and of the text) so one setting looks the same on 9:16 and 16:9. The
  words are rendered into the PNG rather than laid over the preview with CSS —
  the file you upload is the thing that has to carry them.
* **The picture and the words are two files.** `youtube-thumbnail-base.png` is
  the picture; `youtube-thumbnail.png` is it with the words on. Re-typing the
  words never re-does the picture (no second image generation, no second frame
  grab), and picking a different picture keeps the words. A thumbnail made
  before this existed is adopted as its own base the first time words are
  added, so nothing has to be re-picked.
* **The YouTube text and thumbnail know the shape of the video.** The
  **YouTube** button writes a title, a description and tags, and draws a
  thumbnail, from this task's own saved `video_aspect` — not from whatever
  `config.toml` says now. A 9:16 video is packaged as a Short and its
  thumbnail is drawn 1080×1920; a 16:9 video is packaged as a normal video and
  its thumbnail is drawn 1280×720, the size YouTube recommends. The panel says
  which before you press anything, because the shape is the one thing you
  cannot fix afterwards: a landscape thumbnail on a vertical Short is cropped
  from both sides, which is exactly where the subject usually is. The
  thumbnail deliberately ignores `openai_image_size` — that setting exists for
  scene materials and is often portrait.
* **Nothing in that panel reaches the video.** The text and the thumbnail are
  stored with the task and are never read by the render, so they can be
  written while the video is rendering. Over-long text is refused with the
  count rather than silently truncated, and the thumbnail is drawn from the
  description in the panel, which you can rewrite before pressing the button.
* **A scene can lose its file** if something outside the app deletes it. The
  panel says so for that row, and Render refuses before queueing rather than
  failing halfway through.

Design notes and the known limits are in
[video-editor-plan.md](video-editor-plan.md).
