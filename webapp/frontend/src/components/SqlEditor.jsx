import { useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { sql, SQLite } from "@codemirror/lang-sql";

// A focused SQL editor. Cmd/Ctrl-Enter submits — fast keyboard-first interaction.
export default function SqlEditor({ value, onChange, onSubmit, schema, placeholder }) {
  const [focused, setFocused] = useState(false);
  return (
    <div className={"editor-wrap cm-theme" + (focused ? " focus" : "")}>
      <CodeMirror
        value={value}
        height="160px"
        placeholder={placeholder || "SELECT … write your query, then press ⌘↵"}
        extensions={[sql({ dialect: SQLite, schema, upperCaseKeywords: true })]}
        basicSetup={{ lineNumbers: true, foldGutter: false, highlightActiveLine: true }}
        onChange={onChange}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            onSubmit?.();
          }
        }}
      />
    </div>
  );
}
