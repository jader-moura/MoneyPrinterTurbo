---
name: scene-plan
description: Write a scene plan for a MoneyPrinterTurbo video - the narration divided into scenes, one image prompt per scene, which scenes are drawn from a real photo (image-to-image) instead of from text, and the direction of the edit (title, title cards, pauses, impacts, music dropouts, ambience) - and export it as a JSON file to paste into the editor's new-video form. Also add or change the direction of a video that already exists, without re-recording it. Use when asked to plan, storyboard, direct or divide a video into scenes, to turn a topic or a finished script into a scene plan, or to add a title, cards, impacts, silence or ambience to an existing video.
---

# Scene plan

A scene plan does, outside the app, the things the app's own language model
otherwise does: divide the narration into scenes, write one picture prompt per
scene, and choose which scenes are drawn from a real photo. It also **directs
the edit**: where the title appears, which scenes carry a title card, where the
narrator pauses, where a low impact lands, where the music drops out, and where
wind or room tone sits under the picture. The app then records the narration,
draws the pictures and renders the video from it.

The user pastes the exported JSON into the editor's new-video form, under
**More script settings → Scene plan (written outside the app)**.

If the video already exists and the user only wants its direction added or
changed, do not write a new plan: go to
[Directing a video that already exists](#directing-a-video-that-already-exists).

## What you need from the user

- A topic, or a finished script. If they give a script, keep its wording:
  divide it, do not rewrite it.
- If you write the script yourself: the length they want (minutes) and the
  language. Check facts, names, dates and places before writing them; a
  documentary with a wrong date is worse than no documentary.
- Optionally, roughly how many scenes should come from real photos. Without a
  number, aim for about one scene in ten, and never force it (see below).

Ask only for what is missing. Do not ask about picture style: the user chooses
it in the form and the app appends it to every prompt.

## The file

When you are working inside the MoneyPrinterTurbo repository, write the plan to
`storage/scene_plans/<short-slug>.json`. Anywhere else (a chat, an artifact),
give the plan as one JSON file or one copyable JSON block, with nothing but the
JSON in it. The format is the same:

```json
{
  "subject": "The Green River Killer",
  "scenes": [
    {
      "narration": "It is November thirtieth, two thousand one. A truck painter drives to work, the way he has for decades.",
      "prompt": "A 52-year-old white man with thinning brown-grey hair and a neat moustache, in a plaid work shirt, driving a 1990s pickup truck along a wet road lined with pine trees near Seattle, Washington, on a grey November morning in 2001",
      "image": "text"
    },
    {
      "narration": "His name is Gary Ridgway.",
      "prompt": "A 52-year-old white man with thinning brown-grey hair, a neat moustache and large glasses, in a plaid work shirt, looking calmly ahead, Washington state, 2001",
      "image": "photo",
      "photo_kind": "person",
      "photo_name": "Gary Ridgway",
      "photo_search": "Gary Ridgway 2001 arrest"
    }
  ]
}
```

- `subject` — the video's title or topic.
- `narration` — exactly what is spoken over this picture.
- `prompt` — what the picture shows, in English.
- `image` — `"text"` (drawn from the prompt alone) or `"photo"` (the user picks
  a real photo and the picture is redrawn from it in the video's style).
- For `"photo"` scenes: `photo_kind` is `person`, `place` or `thing`;
  `photo_name` is the real name; `photo_search` is what to type into an image
  search to find it.

## Dividing the narration

- One scene is one picture. Aim for **3 to 7 seconds** of speech, about 8 to
  18 words. Over 9 seconds a still picture drags; split the sentence at a
  natural clause instead. Under 2 seconds flickers; join it to its neighbour.
- A new scene starts when what the viewer should **see** changes: a new
  person, place, time, action or idea. Two short sentences about the same
  moment belong in one scene.
- Every scene's narration must end with a full stop, question mark or
  exclamation mark. Each scene is spoken as its own line.
- Write for the ear, because a voice reads it: spell numbers and years the way
  they are said ("nineteen eighty two", "forty nine women"), no brackets, no
  headings, no emoji, no stage directions.
- The scenes, read in order, are the whole script. Nothing is spoken that is
  not in a scene.

## Writing the prompts

Each picture is drawn separately by a model that sees one prompt and nothing
else. Write every prompt to stand alone.

1. One sentence, roughly 15 to 40 words, concrete and physically depictable.
2. **Show people doing something.** Name who they are and the action. A bare
   object ("a bowl of rice") is a stock photo; if the line really is about an
   object, show a hand or a person with it.
3. **Put the feeling on a face** when the line carries one ("nobody expected
   this"): show someone astonished, exhausted, afraid.
4. **Repeat the setting in every prompt**: the year or decade, the country or
   city, the season. Name period clothing and equipment where they matter.
   A scene set in 1982 has no smartphones or flat screens. When the narration
   moves to another year, use that year for those scenes only.
5. **Describe a recurring person in full every time** (age, build, hair,
   clothing), in the same words. Never write "the same man" or "as before";
   the model has never seen the other pictures.
6. **No written text in the picture.** The image model cannot spell. No
   newspaper front pages, signs, name plates, certificates, calendars, maps
   with place names, or screens showing text. For a headline or a document,
   show people handling it: hands holding a folder, a crowd reading a notice
   from too far away to read.
7. Never ask for a portrait labelled with a real person's name. Describe what
   the person looks like and what they are doing.
8. No style words, camera directions, lighting or render instructions; the
   app adds the chosen style.
9. Do not quote the narration in the prompt.
10. Stay respectful with real victims and crimes: no gore, no depiction of the
    act of violence; show the place, the investigators, the aftermath.

## Choosing the photo scenes

A photo scene is worth it only when the narration **names something real that
a viewer would want to see as it really was**, and a photo of it is likely to
exist:

- a named real person (most valuable, choose these first);
- a named real place or building as it looked then;
- a specific real object (a vehicle, a weapon recovered, a famous artefact).

Do not mark a scene as a photo scene to reach a quota. "Inflation rose" has no
photo. Spread the photo scenes through the video rather than bunching them,
and do not use the same person's photo for neighbouring scenes.

A photo scene still needs a full `prompt`: it tells the redraw what the
finished picture should show, and it is what gets drawn if the user skips the
photo. `photo_search` should be specific enough to find the right image:
name, year, and context ("Renton Washington Kenworth truck plant 1980s").

## Directing the edit

Without direction, every scene gets the same treatment from the first second
to the last: one music level, no silence, no emphasis. Direct every plan unless
the user asks for a plain one. All of it is optional, and all of it goes in the
same file:

```json
{
  "subject": "John Wayne Gacy",
  "title": {"text": "John Wayne Gacy", "tagline": "The house on Summerdale Avenue", "scene": 12},
  "scenes": [
    {
      "narration": "December, nineteen seventy eight. A quiet ranch house just outside Chicago.",
      "prompt": "...",
      "card": {"title": "December 1978", "subtitle": "West Summerdale Avenue"},
      "ambience": "wind"
    },
    {
      "narration": "He starts to dig.",
      "prompt": "...",
      "music": "off",
      "ambience": "room",
      "pause_after": 1.0
    },
    {
      "narration": "Only a few inches down, his shovel finds something that is not soil. It is human remains.",
      "prompt": "...",
      "music": "off",
      "ambience": "room",
      "impact": "end"
    }
  ]
}
```

| Field | Where | What it does |
| --- | --- | --- |
| `title` | top level | The video's title, large and centred over a dimmed picture, for as long as scene number `scene` (counting from 1) is spoken. `text` is at most 40 characters; `tagline` is one short line under it. A low impact lands with it. |
| `card` | a scene | A small card in the top-left corner for six seconds from the start of this scene: `title` (at most 40 characters) and `subtitle` (at most 60). |
| `pause_after` | a scene | Seconds of real silence recorded after this scene's narration, 0.2 to 4. The picture stays on screen through it. |
| `impact` | a scene | A low hit: `"end"` lands just after the scene's last word, `"start"` just before its first. |
| `music` | a scene | `"off"`: no music under this scene. It fades out before the scene and returns after it; neighbouring `"off"` scenes make one stretch. |
| `ambience` | a scene | `"wind"` (exteriors, cold, open places) or `"room"` (a low, close interior tone) under this scene. Neighbouring scenes with the same ambience make one stretch. |

How to use them:

- **Title.** Put it on the line that ends the cold open and sends the story
  back to the start ("To answer that, we have to go back to the beginning."),
  not on the first scene: the first scene is the hook. `text` is the name of
  the person or the case; `tagline` is the angle. The title scene cannot also
  carry a card.
- **Cards.** About one every ninety seconds at most: three to six in a
  five-minute video, up to nine or ten in a fifteen-minute one. A card names
  what the viewer needs to hold on to: a date and place when the story jumps in
  time, a person when they enter, a location that matters. Put the card on the
  scene where the name or date is spoken. Leave at least seven seconds between
  two cards. Write years as digits on a card ("December 1978"), though the
  narration spells them out.
- **A card is a claim.** It is on screen in large type with nobody's voice to
  soften it. Write on it only what the narration says, or what you have
  checked as carefully as the script: a person's office and dates, a place, a
  year. If you cannot check a subtitle, leave it out and keep the name alone.
  Tell the user which card texts are not taken from the narration.
- **Pauses.** One second after a line that should hang in the air ("He never
  came back."), half a second before a turn. Five to ten in a fifteen-minute
  video; a pause after every line is just a slow narrator. Never pause in the
  middle of a thought.
- **Impacts.** At most one for every two minutes, on the lines the story turns
  on: the discovery, the name, the evidence that breaks the case, the verdict.
  They stop landing when there are many. An impact works best at the `"end"` of
  the line that delivers the fact, with the music already off.
- **Music off.** For the two or three sequences that should feel exposed: the
  moment of discovery, a disappearance, a death. Mark every scene of the
  sequence, usually three to six scenes. What matters is the time: the music
  takes four seconds to fade, so a stretch under about six seconds is
  swallowed by the fade, and one short scene alone is never enough. A shorter
  silence of two or three scenes works in one place, right before the title.
  The music takes a few seconds to return, so do not mark sequences closer
  than about twenty seconds apart. A longer video can carry one more sequence,
  for example the closing lines.
- **Ambience.** Under a music-off sequence, so the silence is a place and not a
  dropout: `"room"` for a crawl space, a cell, an interview room; `"wind"` for a
  winter street, a riverbank, a graveyard. Also under establishing exteriors.
  It is quiet by design; do not put it under every scene.
- **Respect.** With real victims, direction should make the moment quieter,
  not louder. No impact on a victim's name or on a death itself; drop the music
  and pause instead.

What the app needs for it to work, which you should tell the user:

- a **Finish look** must be chosen in the new-video form. Without one the
  classic renderer runs: pauses are still recorded, everything else in the
  direction is ignored.
- **Pauses** are recorded with Edge voices and with Kokoro. Other voices read
  straight through.
- If a scene's narration is rewritten in the editor and re-recorded, the
  direction on that scene is skipped, because it is found by its words.

## Check it, then hand it over

The app refuses a plan that breaks any of these, so check them yourself first:

- it is valid JSON: one object with a `scenes` list, straight double quotes,
  no comments, no trailing commas, quotes inside text escaped as `\"`;
- between 1 and 600 scenes;
- every scene has a non-empty `narration` and a non-empty `prompt`;
- every `narration` ends with a full stop, question mark or exclamation mark;
- `image` is exactly `"text"` or `"photo"`;
- every `"photo"` scene has `photo_kind` (`person`, `place` or `thing`) and at
  least one of `photo_name` and `photo_search`;
- `pause_after` is a number from 0.2 to 4; `impact` is `"start"` or `"end"`;
  `music` is `"off"` or absent; `ambience` is `"wind"` or `"room"`;
- every `card` has a `title` of at most 40 characters; the `title` has a
  `text` of at most 40 characters and a `scene` that exists and has no card;
- the whole file is under 400,000 characters.

Inside the repository, also run the checker; it reads the file the same way
the app will:

```
.venv/Scripts/python.exe -m app.services.scene_plan storage/scene_plans/<short-slug>.json
```

It prints the number of scenes, the estimated length, which scenes are drawn
from photos, any scene that holds one picture too long, and a `direction:`
line listing the title, cards, pauses, impacts, music-off scenes and ambience
it read. Under that line it remarks on direction that breaks the rules above:
more cards or impacts than the length carries, two cards on screen at once, a
music-off stretch too short to hear, two stretches the music cannot return
between. Those are remarks, not refusals: fix each one, or be able to say why
it is right here. Fix what it reports and run it again until it passes; read
the direction line against what you meant. Outside the repository, estimate the
length yourself at about 2.3 spoken words per second.

Then tell the user:

- where the plan is (the file's path, or the block to copy), the number of
  scenes, the estimated length, and how many scenes are drawn from photos;
- what you directed, in one or two lines: the title, how many cards, pauses
  and impacts, and which sequences play without music;
- to choose a Finish look in the form, since the direction needs it;
- to open the editor's new-video form, leave the script box empty, open
  **More script settings**, and paste the file's contents into **Scene plan**;
- that the video stops before any picture is drawn, so they can read the
  prompts, pick the photos, and change anything first;
- that once the narration is recorded, the editor plays the direction back:
  the title and cards appear over the preview, the impacts, ambience and
  music dropouts are heard, and a **Direction** track on the timeline shows
  where each one lands. The first time it takes a few minutes to appear,
  while the app times it against the recording. The colour grade and the
  final sound balance are only in the rendered video.

Do not start the video yourself, and do not call the app's API.

## Directing a video that already exists

When the narration is recorded and the user wants a title, cards or sound
added or changed, the scenes stay as they are. You write a **direction file**
that says what goes on which scene, and one command stores it with the video.
This only works for a video that was made from a scene plan.

1. List the video's scenes. The task is the id in the editor's address
   (`...?task=<id>`); the whole address works too:

   ```
   .venv/Scripts/python.exe -m app.services.direction_preview <task>
   ```

   Each line is a scene number, roughly when it is spoken, the direction it
   already carries in square brackets, and its narration. Read all of it before
   choosing anything.

2. Write the direction file next to the plans, as
   `storage/scene_plans/<short-slug>.direction.json`:

   ```json
   {
     "title": {"text": "The Cadaver Synod", "tagline": "Rome, 897", "scene": 8},
     "scenes": {
       "1": {"card": {"title": "January 897", "subtitle": "Rome"}},
       "1-7": {"ambience": "room"},
       "5-7": {"music": "off"},
       "48": {"impact": "end"}
     }
   }
   ```

   `scenes` is keyed by scene number, or by a run like `"5-7"`. A scene named
   by several keys gets all of them. The fields are `card`, `impact`, `music`
   and `ambience`, with the same meanings and the same rules as above.

3. Store it:

   ```
   .venv/Scripts/python.exe -m app.services.direction_preview <task> storage/scene_plans/<short-slug>.direction.json
   ```

   It prints the same `direction:` line and remarks as the checker. Fix what it
   remarks on and run it again; running it again is how you change anything.

What to know, and tell the user:

- **The file is the whole direction.** Whatever the video's plan directed
  before is replaced, so when changing one card, start from the earlier
  direction file, or rebuild it from the listing.
- **Pauses cannot be added.** A pause is silence recorded with the voice. The
  command refuses `pause_after`; pauses the video already has are kept. If the
  user wants pauses, the narration has to be recorded again.
- Nothing else changes: not the narration, the pictures or the timeline. The
  video's file as it was before the first change is kept beside it as
  `script.before-direction.json`.
- The editor shows the new direction on its **Direction** track as soon as the
  page is reloaded. The finished video changes only when it is rendered again.
- The command says so if the video has no **Finish look** (nothing would be
  rendered), or if a `title-cards.json` in the video's folder overrides the
  cards.
