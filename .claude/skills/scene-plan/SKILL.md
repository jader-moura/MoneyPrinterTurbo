---
name: scene-plan
description: Write a scene plan for a MoneyPrinterTurbo video - the narration divided into scenes, one image prompt per scene, and which scenes are drawn from a real photo (image-to-image) instead of from text - and export it as a JSON file to paste into the editor's new-video form. Use when asked to plan, storyboard or divide a video into scenes, or to turn a topic or a finished script into a scene plan.
---

# Scene plan

A scene plan does, outside the app, the three things the app's own language
model otherwise does: divide the narration into scenes, write one picture
prompt per scene, and choose which scenes are drawn from a real photo. The app
then records the narration, draws the pictures and renders the video from it.

The user pastes the exported JSON into the editor's new-video form, under
**More script settings → Scene plan (written outside the app)**.

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
- the whole file is under 400,000 characters.

Inside the repository, also run the checker; it reads the file the same way
the app will:

```
.venv/Scripts/python.exe -m app.services.scene_plan storage/scene_plans/<short-slug>.json
```

It prints the number of scenes, the estimated length, which scenes are drawn
from photos, and any scene that holds one picture too long. Fix what it
reports and run it again until it passes. Outside the repository, estimate
the length yourself at about 2.3 spoken words per second.

Then tell the user:

- where the plan is (the file's path, or the block to copy), the number of
  scenes, the estimated length, and how many scenes are drawn from photos;
- to open the editor's new-video form, leave the script box empty, open
  **More script settings**, and paste the file's contents into **Scene plan**;
- that the video stops before any picture is drawn, so they can read the
  prompts, pick the photos, and change anything first.

Do not start the video yourself, and do not call the app's API.
