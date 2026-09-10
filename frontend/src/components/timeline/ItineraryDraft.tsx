import { ItineraryDraft as ItineraryDraftModel } from "../../services/apiClient";

type ItineraryDraftProps = {
  draft: ItineraryDraftModel;
};

export function ItineraryDraft({ draft }: ItineraryDraftProps) {
  return (
    <section aria-label="Itinerary draft">
      <h2>{draft.title}</h2>
      {draft.days.map((day) => (
        <div className="itinerary-day" key={day.dayNumber}>
          <h3>{day.title}</h3>
          <ol className="segment-list">
            {day.segments.map((segment) => (
              <li className="segment-item" key={segment.id}>
                <strong>{segment.startTime}</strong>
                <span>{segment.title}</span>
                <small>
                  {segment.poiName} · {segment.durationMinutes} 分钟 · {segment.transportMode}
                </small>
                <ul>
                  {segment.costItems.map((item) => (
                    <li key={item.label}>
                      {item.label}: {item.amountCny} {item.currency}
                      {item.isEstimate ? "（估算）" : ""}
                    </li>
                  ))}
                  {segment.reservationNotes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </div>
      ))}
    </section>
  );
}
