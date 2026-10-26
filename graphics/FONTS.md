# Fonts used by the compositions (`public/fonts/`)

All three are SIL Open Font License 1.1, which allows use and bundling in a project like this.

| File | Family | Author | Source |
|---|---|---|---|
| `BebasNeue-Regular.ttf` | Bebas Neue | Dharma Type | the ClipBot library's copy (`work/_library/fonts`), https://github.com/google/fonts/tree/main/ofl/bebasneue |
| `Anton-Regular.ttf` | Anton | The Anton Project Authors | the library's copy, https://github.com/google/fonts/tree/main/ofl/anton |
| `ShareTechMono-Regular.woff2` | Share Tech Mono | Carrois Type Design | npm `@fontsource/share-tech-mono` (latin, 400); its licence is `ShareTechMono-LICENSE.txt` |

`src/theme.js` (`FACES`) is the list a render waits on. Adding a file to `public/` changes `editmotion.source_hash()` and so
re-draws every cached graphic once; that is intended.
