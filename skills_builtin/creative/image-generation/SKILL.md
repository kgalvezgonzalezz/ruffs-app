---
name: image-generation
description: Create and edit images with the user's own API key (generate_image / edit_image): pick create vs edit, write a strong prompt, choose the size, keep costs low and handle errors
version: 1.0.0
category: creative
tags: [imagen, imágenes, imagenes, foto, fotos, dibuja, dibújame, dibujo, ilustración, ilustracion, logo, logotipo, wallpaper, fondo, sticker, póster, poster, image, picture, draw, illustration, edita, modifica, agrégale, agregale, cámbiale, cambiale, quítale, quitale]
status: published
confidence: 0.95
source: builtin
---

## When to Use

Use this whenever the user asks to CREATE an image («dibuja…», «genera una imagen de…», «hazme un logo», «un fondo de pantalla», «una ilustración», «una foto de…») or to MODIFY an existing one («modifica esta imagen», «agrégale…», «cámbiale el fondo», «quítale…», «hazla estilo acuarela», «combina estas dos fotos»). The images are made with the user's own API key (OpenRouter or OpenAI) through the `generate_image` and `edit_image` tools (also exposed as `mcp__image_gen__generate_image` / `mcp__image_gen__edit_image`; they are the same tools).

Do NOT use it to describe or analyze a photo (just look at it), to search the web for existing pictures, or to make charts from data (use python/documents for that).

## Procedure

1. Decide CREATE vs EDIT. If the user attached an image, or refers to one already in the chat («esta», «la anterior», «la que hiciste», «ponle…», «cámbiale…»), call `edit_image`. Otherwise call `generate_image`.
2. Write the prompt in ENGLISH (image models follow English best), even if the user wrote in Spanish, and make it specific: subject, action, setting, art style or medium, composition and framing, lighting, color palette, mood. If text must appear in the image, put the exact words in quotes and say which language («the sign reads "Feliz cumpleaños"»). Add only details that serve what the user asked; do not change their idea.
3. Choose the size from the use: phone wallpaper, story or vertical poster → `9:16`; desktop wallpaper, banner, landscape → `16:9`; logo, icon, avatar, sticker, profile photo → `1:1`; printed poster/flyer → `2:3`. If unsure, omit it (square).
4. For EDIT: pass `images` with the attachment id shown as «[Image attached: name (id: X)]» or «[Image: name (id: X)]», or the `/api/generated-image/…` URL from a previous result, or `["last"]` for the most recent image in this chat. Up to 4 images (to combine them). The prompt must say exactly what changes AND what must stay the same («Add a red party hat to the dog. Keep the dog, pose, background and lighting exactly the same.»).
5. Make ONE image per call and one call per request. Each image costs real money on the user's key (about US$0.04 with google/gemini-2.5-flash-image; GPT image at high quality can cost several times more). Only make several versions if the user asks for options.
6. Leave `model` empty so the one configured in Settings → AI Defaults → Image Generation is used, unless the user names a specific model.
7. After the tool returns, the image ALREADY appears in the chat: do not paste the URL, markdown image or base64. Reply in the user's language with one or two short lines (what you made, the cost if the tool reported it) and offer a concrete tweak («¿Lo quieres con fondo blanco o más colorido?»).

## Pitfalls

- Never draw images with bash/python (PIL, matplotlib, SVG) when the user wants a picture; use the image tools.
- Never invent attachment ids or image URLs; if you cannot find the image to edit, ask the user to attach it again.
- If you cannot see the generated image (text-only model), do not describe details you have not seen; describe what you asked for.
- "No image model found": tell the user to choose one in Settings → AI Defaults → Image Generation — with an OpenRouter key `google/gemini-2.5-flash-image` (cheap, edits well); with an OpenAI key `gpt-image-1`. dall-e-3 cannot edit.
- HTTP 402 / insufficient credits: the user must add credit to their provider account; do not retry.
- The provider refused the prompt (safety): explain briefly and offer a respectful alternative; do not retry the same prompt. Do not make deceptive or sexual images of real people, or try to bypass the provider's rules.
- Retry at most once on a timeout; then suggest `quality: low` or trying again later.

## Verification

- The tool output contains `image_url: /api/generated-image/…` (and `Costo: US$ …` with OpenRouter).
- For an edit, the result says «Edité la imagen…»; if it lists images it could not find, tell the user.
