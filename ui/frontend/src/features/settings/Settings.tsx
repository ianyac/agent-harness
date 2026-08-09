import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useLayoutEffect, useRef } from "react";

import type { PreferenceChanges, Preferences } from "./preferences";
import styles from "./settings.module.css";

type SettingsProps = {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly preferences: Preferences;
  readonly onChange: (changes: PreferenceChanges) => void;
  readonly workspace: string | null;
  readonly logsSupported: boolean;
};

const shortcuts = [
  ["New chat", "⌘N"],
  ["Search commands and sessions", "⌘K"],
  ["Search conversation", "⌘F"],
  ["Toggle activity", "⌘⇧I"],
  ["Send message", "⌘Enter"],
  ["Close transient UI or focus Stop", "Escape"],
] as const;

export function Settings({
  open,
  onOpenChange,
  preferences,
  onChange,
  workspace,
  logsSupported,
}: SettingsProps) {
  const focusOrigin = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    if (open && document.activeElement instanceof HTMLElement && document.activeElement !== document.body) {
      focusOrigin.current = document.activeElement;
    }
  }, [open]);

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={styles.dialog}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const origin = focusOrigin.current;
            focusOrigin.current = null;
            if (origin?.isConnected) origin.focus();
          }}
        >
          <header className={styles.header}>
            <div>
              <Dialog.Title className={styles.title}>Settings</Dialog.Title>
              <Dialog.Description className={styles.description}>
                Local preferences for the daily driver.
              </Dialog.Description>
            </div>
            <Dialog.Close className={styles.iconButton} aria-label="Close settings">
              <X aria-hidden="true" size={18} />
            </Dialog.Close>
          </header>

          <div className={styles.scrollArea}>
            <fieldset className={styles.section}>
              <legend>Appearance</legend>
              <div className={styles.inlineChoices}>
                {(["system", "light", "dark"] as const).map((appearance) => (
                  <label key={appearance} className={styles.choice}>
                    <input
                      type="radio"
                      name="appearance"
                      value={appearance}
                      checked={preferences.appearance === appearance}
                      onChange={() => onChange({ appearance })}
                    />
                    {appearance[0].toUpperCase() + appearance.slice(1)}
                  </label>
                ))}
              </div>
            </fieldset>

            <section className={styles.section} aria-labelledby="session-defaults-heading">
              <h2 id="session-defaults-heading">New session defaults</h2>
              <label className={styles.field}>
                <span>Default permission mode</span>
                <select
                  value={preferences.defaultMode}
                  onChange={(event) => onChange({
                    defaultMode: event.target.value as Preferences["defaultMode"],
                  })}
                >
                  <option value="default">Default</option>
                  <option value="acceptAll">Accept all</option>
                  <option value="readOnly">Read only</option>
                </select>
              </label>
              <fieldset className={styles.nestedFieldset}>
                <legend>Context management</legend>
                <label className={styles.choice}>
                  <input
                    type="radio"
                    name="context-mode"
                    checked={preferences.contextMode === "compaction"}
                    onChange={() => onChange({ contextMode: "compaction" })}
                  />
                  Standard compaction
                </label>
                <label className={styles.choice}>
                  <input
                    type="radio"
                    name="context-mode"
                    checked={preferences.contextMode === "folding"}
                    onChange={() => onChange({ contextMode: "folding" })}
                  />
                  Recoverable folding
                </label>
              </fieldset>
              <p className={styles.help}>Defaults apply to future sessions only.</p>
            </section>

            <fieldset className={styles.section}>
              <legend>Notifications and sidebar</legend>
              <label className={styles.toggle}>
                <input
                  type="checkbox"
                  checked={preferences.notifyPermissions}
                  onChange={(event) => onChange({ notifyPermissions: event.target.checked })}
                />
                Notify for permission requests
              </label>
              <label className={styles.toggle}>
                <input
                  type="checkbox"
                  checked={preferences.notifyCompletions}
                  onChange={(event) => onChange({ notifyCompletions: event.target.checked })}
                />
                Notify when background work completes
              </label>
              <label className={styles.toggle}>
                <input
                  type="checkbox"
                  checked={preferences.sidebarCollapsed}
                  onChange={(event) => onChange({ sidebarCollapsed: event.target.checked })}
                />
                Collapse sidebar
              </label>
            </fieldset>

            <section className={styles.section} aria-labelledby="shortcuts-heading">
              <h2 id="shortcuts-heading">Keyboard shortcuts</h2>
              <dl className={styles.shortcutList}>
                {shortcuts.map(([label, shortcut]) => (
                  <div key={label}><dt>{label}</dt><dd><kbd>{shortcut}</kbd></dd></div>
                ))}
              </dl>
            </section>

            <section className={styles.section} aria-labelledby="data-heading">
              <h2 id="data-heading">Local data locations</h2>
              <dl className={styles.pathList}>
                <div><dt>Codex sign-in file</dt><dd><code>~/.codex/auth.json</code></dd></div>
                {workspace === null || workspace === "" ? null : (
                  <div><dt>Workspace sessions</dt><dd><code>{workspace}/.agent/sessions</code></dd></div>
                )}
                {logsSupported ? <div><dt>Service logs</dt><dd>Available through Open logs.</dd></div> : null}
              </dl>
              <p className={styles.help}>The UI never reads or stores credential contents.</p>
            </section>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
