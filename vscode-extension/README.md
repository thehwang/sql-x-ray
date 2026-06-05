# SQLucent — SQL X-Ray (VS Code / Cursor extension)

See through any SQL without running it. Right-click a query and get an
interactive data-flow diagram — click any node to inspect its SQL, sources,
operations, and outputs.

This extension is a thin shell over the [`sqlucent`](https://pypi.org/project/sqlucent/)
engine: it shells out to the CLI and renders its self-contained `--html` output
in a webview. Because Cursor is a VS Code fork, the same `.vsix` installs in both.

## Requirements

The `sqlucent` CLI must be installed and on your `PATH`:

```bash
pip install sqlucent
```

If it lives somewhere not on your `PATH`, set the **`sqlucent.path`** setting to
its full path.

## Usage

- Open a `.sql` file (or select a SQL snippet in any file), then:
  - **Right-click → "SQLucent: Explain this SQL"**, or
  - run **"SQLucent: Explain this SQL"** from the Command Palette, or
  - **right-click a `.sql` file in the Explorer**.
- A panel opens beside your editor with the interactive data-flow diagram.

What gets analyzed:

| Invoked on | Analyzed |
|---|---|
| A selection | the selected SQL |
| A saved, unmodified file | the file on disk |
| An unsaved / dirty editor | the current on-screen text |
| A `.sql` file in the Explorer | that file |

## Settings

| Setting | Default | Description |
|---|---|---|
| `sqlucent.path` | `sqlucent` | Path to the `sqlucent` executable. |
| `sqlucent.dialect` | `bigquery` | SQL dialect passed to the engine. |

## Develop

```bash
npm install
npm run compile     # or: npm run watch
```

Press <kbd>F5</kbd> in VS Code to launch an Extension Development Host. To build a
shareable package, `npx @vscode/vsce package` produces a `.vsix` you can install
in VS Code or Cursor via "Install from VSIX…".

## License

MIT
