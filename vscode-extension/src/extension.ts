import * as path from "path";
import { spawn } from "child_process";
import * as vscode from "vscode";

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("sqlucent.explainSql", (uri?: vscode.Uri) =>
      explainSql(uri).catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        void vscode.window.showErrorMessage(`SQLucent: ${message}`);
      })
    )
  );
}

export function deactivate(): void {
  // no-op
}

interface Invocation {
  args: string[];
  stdin?: string;
  title: string;
}

/** Decide what SQL to analyze: an explorer .sql file, a selection, or the open doc. */
function resolveInvocation(uri?: vscode.Uri): Invocation {
  // Invoked from the Explorer context menu on a .sql file.
  if (uri?.fsPath && uri.fsPath.toLowerCase().endsWith(".sql")) {
    return { args: [uri.fsPath], title: path.basename(uri.fsPath) };
  }

  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    throw new Error("Open a .sql file or select some SQL first.");
  }

  const selection = editor.selection;
  if (selection && !selection.isEmpty) {
    return {
      args: ["-"],
      stdin: editor.document.getText(selection),
      title: "SQL X-Ray (selection)",
    };
  }

  // Whole document. Pass via stdin when unsaved so we analyze what's on screen.
  const name = editor.document.isUntitled
    ? "SQL X-Ray"
    : path.basename(editor.document.fileName);
  if (editor.document.isUntitled || editor.document.isDirty) {
    return { args: ["-"], stdin: editor.document.getText(), title: name };
  }
  return { args: [editor.document.uri.fsPath], title: name };
}

async function explainSql(uri?: vscode.Uri): Promise<void> {
  const { args, stdin, title } = resolveInvocation(uri);

  const config = vscode.workspace.getConfiguration("sqlucent");
  const exe = config.get<string>("path", "sqlucent");
  const dialect = config.get<string>("dialect", "bigquery");

  const fullArgs = [...args, "--dialect", dialect, "--html"];

  const html = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "SQLucent: analyzing SQL…" },
    () => runSqlucent(exe, fullArgs, stdin)
  );

  const panel = vscode.window.createWebviewPanel(
    "sqlucentExplain",
    title,
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  // The page emitted by `--html` is fully self-contained (Mermaid inlined), so
  // it renders offline with no external resource roots needed.
  panel.webview.html = html;
}

function runSqlucent(exe: string, args: string[], stdin?: string): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const proc = spawn(exe, args, { cwd: workspaceCwd() });

    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (chunk: Buffer) => (stdout += chunk.toString()));
    proc.stderr.on("data", (chunk: Buffer) => (stderr += chunk.toString()));

    proc.on("error", (err: NodeJS.ErrnoException) => {
      if (err.code === "ENOENT") {
        reject(
          new Error(
            `Could not find '${exe}'. Install it with \`pip install sqlucent\`, ` +
              "or set the `sqlucent.path` setting to its full path."
          )
        );
        return;
      }
      reject(err);
    });

    proc.on("close", (code: number | null) => {
      if (code === 0) {
        resolve(stdout);
      } else {
        reject(new Error(stderr.trim() || `sqlucent exited with code ${code ?? "unknown"}.`));
      }
    });

    if (stdin !== undefined) {
      proc.stdin.write(stdin);
      proc.stdin.end();
    }
  });
}

function workspaceCwd(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}
