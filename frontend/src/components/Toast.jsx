import { createContext, useCallback, useContext, useRef, useState } from "react";
import { CheckCircle2, XCircle, Info } from "lucide-react";

const ToastContext = createContext(null);

const ICONS = { success: CheckCircle2, error: XCircle, info: Info };
const COLORS = {
  success: "border-approve/30 text-approve bg-approve/[0.06]",
  error: "border-block/30 text-block bg-block/[0.06]",
  info: "border-accent/30 text-accent bg-accent/[0.06]",
};

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const idRef = useRef(0);

  const dismiss = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const push = useCallback((message, type = "info", duration = 3500) => {
    const id = ++idRef.current;
    setToasts((t) => [...t, { id, message, type }]);
    if (duration > 0) {
      setTimeout(() => dismiss(id), duration);
    }
    return id;
  }, [dismiss]);

  return (
    <ToastContext.Provider value={{ push, dismiss }}>
      {children}
      <div
        className="fixed bottom-5 right-5 z-50 flex flex-col gap-2 w-[320px]"
        role="status"
        aria-live="polite"
        aria-atomic="false"
      >
        {toasts.map((t) => {
          const Icon = ICONS[t.type] || Info;
          return (
            <div
              key={t.id}
              className={`animate-slidein flex items-start gap-2.5 rounded-lg border px-3.5 py-3 text-[13px] font-medium shadow-card backdrop-blur-sm bg-surface/95 ${COLORS[t.type] || COLORS.info}`}
            >
              <Icon size={16} strokeWidth={2} className="shrink-0 mt-0.5" />
              <span className="text-text leading-snug">{t.message}</span>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
