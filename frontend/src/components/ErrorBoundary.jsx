import { Component } from "react";
import { AlertTriangle } from "lucide-react";

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Real deployments should ship this to an actual error tracker --
    // console.error is the honest floor for a demo build, not a
    // silent swallow.
    console.error("Sentinel UI crashed:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-bg px-6">
          <div className="max-w-md w-full flex flex-col items-center text-center gap-3 border border-block/25 bg-block/[0.04] rounded-xl p-8">
            <div className="w-10 h-10 rounded-full bg-block/10 flex items-center justify-center">
              <AlertTriangle size={18} strokeWidth={2} className="text-block" />
            </div>
            <div className="text-text font-semibold text-sm">Something went wrong in the UI</div>
            <div className="text-textfaint text-xs leading-relaxed">
              This section hit an unexpected error. Your data on the backend is unaffected — reloading usually
              resolves it.
            </div>
            <button
              onClick={() => window.location.reload()}
              className="mt-2 px-4 py-2 rounded-lg text-[13px] font-medium bg-accent text-white hover:opacity-90 transition-opacity"
            >
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
