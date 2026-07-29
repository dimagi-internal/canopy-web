// Voice dictation for the composer, via the Web Speech API.
//
// A web page cannot switch the OS keyboard into its mic mode (there is no API
// for that on Android or iOS), so the mic button does the better thing:
// SpeechRecognition transcribes directly into the draft, no keyboard involved.
// Where the API doesn't exist the hook reports unsupported and the button
// simply isn't rendered — nothing else degrades.

import { useCallback, useEffect, useRef, useState } from "react";

// lib.dom ships no SpeechRecognition types (the API is prefixed in every
// browser that has it), so we carry the minimal shape we touch.
interface RecognitionAlternativeLike {
  transcript: string;
}
interface RecognitionResultLike {
  isFinal: boolean;
  0: RecognitionAlternativeLike;
  length: number;
}
interface RecognitionResultEventLike {
  resultIndex: number;
  results: ArrayLike<RecognitionResultLike>;
}
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: RecognitionResultEventLike) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export interface DictationHandle {
  /** False where the browser has no SpeechRecognition — hide the mic. */
  supported: boolean;
  listening: boolean;
  /** The not-yet-final transcript of the phrase being spoken right now. */
  interim: string;
  toggle: () => void;
  stop: () => void;
}

/**
 * `onFinal` receives each finalized utterance (already trimmed); the caller
 * appends it to the draft. Interim text is exposed for display only and never
 * written into the draft — the co-edited draft syncs every keystroke to the
 * server, and streaming provisional recognition through it would churn the
 * draft version on text that is about to be rewritten.
 */
export function useDictation(onFinal: (text: string) => void): DictationHandle {
  const supported = recognitionCtor() != null;
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const recRef = useRef<SpeechRecognitionLike | null>(null);
  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;

  const stop = useCallback(() => {
    recRef.current?.stop();
  }, []);

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor || recRef.current) return;
    const rec = new Ctor();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang =
      typeof navigator !== "undefined" && navigator.language
        ? navigator.language
        : "en-US";
    rec.onresult = (event) => {
      const finals: string[] = [];
      let interimText = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const transcript = result[0]?.transcript ?? "";
        if (result.isFinal) finals.push(transcript.trim());
        else interimText += transcript;
      }
      const finalText = finals.filter(Boolean).join(" ");
      if (finalText) onFinalRef.current(finalText);
      setInterim(interimText.trim());
    };
    // onend is the single funnel for every way a run stops: our stop(), a
    // recognition error (the browser fires end after error), or Chrome ending
    // the run itself after sustained silence.
    rec.onend = () => {
      recRef.current = null;
      setListening(false);
      setInterim("");
    };
    try {
      rec.start();
    } catch {
      return; // double-start race → InvalidStateError; already listening
    }
    recRef.current = rec;
    setListening(true);
  }, []);

  const toggle = useCallback(() => {
    if (recRef.current) stop();
    else start();
  }, [start, stop]);

  useEffect(() => {
    return () => recRef.current?.abort();
  }, []);

  return { supported, listening, interim, toggle, stop };
}
