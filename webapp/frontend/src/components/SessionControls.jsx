// Pause / End test / Resume / Reopen controls for a class's live session. Shared by the
// Insights live view and each Classes row. Each button carries a data-tip explaining exactly
// what it does so the pause-vs-end distinction is always one hover away.
export default function SessionControls({ sessionState, onChangeSession, size = "sm" }) {
  const ghost = "btn ghost " + size;
  const danger = "btn danger " + size;

  if (sessionState === "ended") {
    return (
      <div className="session-controls">
        <button
          className={ghost}
          data-tip="Reopen the test for everyone — students can switch questions, submit, and ask for hints again."
          onClick={() => onChangeSession("running")}
        >Reopen</button>
      </div>
    );
  }

  return (
    <div className="session-controls">
      {sessionState === "paused" ? (
        <button
          className={ghost}
          data-tip="Resume — students can submit and ask for hints again."
          onClick={() => onChangeSession("running")}
        >Resume</button>
      ) : (
        <button
          className={ghost}
          data-tip="Pause — freezes the test for everyone: no submitting and no hints until you resume."
          onClick={() => onChangeSession("paused")}
        >Pause</button>
      )}
      <button
        className={danger}
        data-tip="End test — students may submit only the question they're on. No hints, and no switching to other questions."
        onClick={() => onChangeSession("ended")}
      >End test</button>
    </div>
  );
}
