import React from "react";

type PageEdge = "start" | "end";

export async function waitForLayout(frameCount: number): Promise<void> {
  await new Promise<void>((resolve) => {
    let remaining = frameCount;
    const next = () => {
      if (remaining <= 0) {
        resolve();
        return;
      }
      remaining -= 1;
      window.requestAnimationFrame(next);
    };
    window.requestAnimationFrame(next);
  });
}

export function useUnderfillPagination(
  scrollRef: React.RefObject<HTMLElement | null>,
  hasMoreAtStart: boolean,
  hasMoreAtEnd: boolean,
  isLoadingMore: boolean,
  loadMore: (edge: PageEdge) => Promise<number>
): React.WheelEventHandler<HTMLElement> {
  const recoveringRef = React.useRef(false);
  const generationRef = React.useRef(0);
  const stateRef = React.useRef({ hasMoreAtStart, hasMoreAtEnd, isLoadingMore, loadMore });
  stateRef.current = { hasMoreAtStart, hasMoreAtEnd, isLoadingMore, loadMore };

  React.useEffect(() => () => {
    generationRef.current += 1;
    recoveringRef.current = false;
  }, []);

  return React.useCallback((event) => {
    const initialElement = event.currentTarget;
    if (initialElement.scrollHeight > initialElement.clientHeight + 1 || recoveringRef.current) return;
    const initialState = stateRef.current;
    if ((!initialState.hasMoreAtStart && !initialState.hasMoreAtEnd) || initialState.isLoadingMore) return;
    event.preventDefault();
    recoveringRef.current = true;
    const generation = generationRef.current;
    void (async () => {
      try {
        for (let attempt = 0; attempt < 8 && generationRef.current === generation; attempt += 1) {
          const element = scrollRef.current;
          if (!element || element.scrollHeight > element.clientHeight + 1) return;
          const state = stateRef.current;
          if (state.isLoadingMore) {
            await waitForLayout(2);
            continue;
          }
          const edge = state.hasMoreAtStart ? "start" : state.hasMoreAtEnd ? "end" : null;
          if (!edge || await state.loadMore(edge) <= 0) return;
          await waitForLayout(6);
        }
      } finally {
        recoveringRef.current = false;
      }
    })();
  }, [scrollRef]);
}
