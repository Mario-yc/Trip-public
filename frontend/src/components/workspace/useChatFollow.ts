import { useCallback, useEffect, useRef, useState } from "react";

export function preferredScrollBehavior(): ScrollBehavior {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? "auto" : "smooth";
}

/** Scroll presentation only: no stream ownership, event subscriptions or business state. */
export function useChatFollow(updateKey: string, sessionId?: string) {
  const ref = useRef<HTMLElement | null>(null);
  const following = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const onScroll = useCallback(() => {
    const node = ref.current;
    if (!node || node.clientHeight === 0) return;
    following.current = node.scrollHeight - node.clientHeight - node.scrollTop < 64;
    setShowLatest(!following.current);
  }, []);
  const scrollLatest = useCallback(() => {
    following.current = true;
    setShowLatest(false);
    const node = ref.current;
    if (node?.scrollTo) node.scrollTo({ top: node.scrollHeight, behavior: preferredScrollBehavior() });
    else if (node) node.scrollTop = node.scrollHeight;
  }, []);
  useEffect(() => {
    following.current = true;
    setShowLatest(false);
  }, [sessionId]);
  useEffect(() => {
    const node = ref.current;
    if (node && node.clientHeight > 0 && following.current) node.scrollTop = node.scrollHeight;
  }, [updateKey, sessionId]);
  return { ref, onScroll, showLatest, scrollLatest };
}
