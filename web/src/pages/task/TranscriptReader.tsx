import { useState, type RefObject, type SyntheticEvent } from "react";
import { Icon } from "../../components/ui";
import { clock } from "../../format";
import type { Transcript } from "../../types";

type Props = {
  assetId: string | null;
  name: string;
  hasVideo: boolean;
  transcript: Transcript | null;
  media: RefObject<HTMLMediaElement | null>;
  time: number;
  setTime: (ms: number) => void;
  seek: (ms: number) => void;
};
export function TranscriptReader({
  assetId,
  name,
  hasVideo,
  transcript,
  media,
  time,
  setTime,
  seek,
}: Props) {
  const [search, setSearch] = useState("");
  const query = search.toLowerCase();
  const segments = (transcript?.segments ?? []).filter((s) =>
    s.text.toLowerCase().includes(query),
  );
  // Media elements use seconds; transcript and evidence ranges are in milliseconds.
  const playerProps = {
    ref: (node: HTMLMediaElement | null) => {
      media.current = node;
    },
    controls: true,
    preload: "metadata" as const,
    src: `/api/v1/assets/${assetId}/media`,
    onTimeUpdate: (e: SyntheticEvent<HTMLMediaElement>) =>
      setTime(e.currentTarget.currentTime * 1000),
  };
  return (
    <div className="reader-main">
      <div className={`player ${hasVideo ? "" : "audio-player"}`}>
        {assetId &&
          (hasVideo ? (
            <video {...playerProps} />
          ) : (
            <>
              <div className="audio-art">
                <div className="brandmark" aria-hidden="true">
                  <span>声</span>
                </div>
                <div>
                  <span className="eyebrow">音频原片</span>
                  <h3>{name}</h3>
                </div>
              </div>
              <audio {...playerProps} />
            </>
          ))}
      </div>
      <section className="card transcript">
        <div className="transcript-head">
          <div>
            <h2>同步文稿</h2>
            <small>
              {transcript
                ? `${transcript.language.toUpperCase()} · ${clock(transcript.duration_ms)} · ${transcript.segments?.length ?? 0} 段`
                : "语音识别完成后，文稿会显示在这里。"}
            </small>
          </div>
          <label className="search">
            <Icon name="search" size={17} />
            <input
              aria-label="搜索文稿"
              placeholder="搜索原话…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
        <div className="segment-list">
          {segments.length ? (
            segments.map((s) => (
              <button
                key={s.segment_id}
                className={`segment ${time >= s.start_ms && time < s.end_ms ? "current" : ""}`}
                onClick={() => seek(s.start_ms)}
              >
                <span>{clock(s.start_ms)}</span>
                <p>
                  {s.text}
                  {s.needs_review && (
                    <small className="review-tag">待核对</small>
                  )}
                </p>
              </button>
            ))
          ) : (
            <div className="empty">
              <Icon name="tasks" size={30} />
              <p>
                {transcript?.warnings.includes("no_speech")
                  ? "没有检测到可转录语音。"
                  : search
                    ? "没有匹配的原话。"
                    : "等待文稿就绪。"}
              </p>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
