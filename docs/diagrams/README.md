# Local diagrams and presentation

The README embeds the exact architecture PNG supplied by the author, shown below. The editable local presentation contains **11 frames** with native Excalidraw shapes, arrows, text, and charts.

![Interaction Agent architecture supplied by the author](openpoke-architecture.png)

| File | Purpose |
| --- | --- |
| [Architecture image](openpoke-architecture.png) | Exact original PNG, embedded in the main README without redrawing or resizing. |
| [Frame 1 preview](openpoke-overload-overview.svg) | Generated preview of the local presentation's first frame. |
| [Full presentation image](openpoke-overload-solutions.svg) | All 11 local frames, including the three evidence charts. |
| [Editable presentation](openpoke-overload-solutions.excalidraw) | Import into Excalidraw to edit native components. Speaker notes are stored in each frame's `customData.speakerNotes`. |
| [Sequence source](openpoke-overload-sequence.mmd) | Exact saved Mermaid source retrieved November 16, 2026. |
| [SVG renderer](render-presentation.mjs) | Regenerates both images from the editable local presentation. |

The local presentation has been rebuilt in the existing purple, blue, and green theme. It is a maintained local version; it is not a fresh export of the hosted Excalidraw canvas. The [hosted presentation](https://link.excalidraw.com/l/2C1urTzZVM0/2hmrnSZUcZZ) and [hosted sequence](https://mermaid.ai/d/d30bea16-7093-473f-971e-fea1f4a4dba9) remain available.

## Keep images current

Edit the local `.excalidraw` file, then run:

```bash
node docs/diagrams/render-presentation.mjs
```

The supplied architecture PNG is preserved as-is and is not regenerated. The SVG overview renders frame 1; the full SVG image renders every frame. Text remains vector text, with a system-font fallback. The images contain no external resources or scripts. The older PNG board preview has been replaced by these scalable images.

## Configuration and evidence

Defaults shown: **100 active globally**, **100 waiting**, **one active execution per EA**, **300-second queue and run deadlines**, **8 EA planning turns**, **5 follow-up stages**, and **20 total submissions** per initiating request.

Slides 8–10 use [committed measurements](../charts/measurements.json). The concurrency illustration preserves its explicit **four-worker fixture**; the updated 100-worker setting is labeled separately as needing a rerun. Slide 11 preserves the recorded **9/10 latest** and **10/10 earlier** results with synthetic email tools. See [validation status and stats](../evaluation/README.md#validation-status).
