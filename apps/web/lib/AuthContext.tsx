"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { login, me, type CurrentUser } from "@/lib/api";

interface AuthContextValue {
  user: CurrentUser | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// Demo-only, opt-in via .env.local (gitignored, never present in a real deployment). When set, an
// unauthenticated visitor is transparently signed into a real, pre-existing demo account through
// the REAL /auth/login endpoint -- this is not a security bypass of any kind, the backend's
// session cookie and get_identity() enforcement are completely unmodified; it just skips showing
// the manual login form for a recruiter/demo viewer who shouldn't need to know or type credentials.
const DEMO_EMAIL = process.env.NEXT_PUBLIC_DEMO_EMAIL;
const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD;

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    let current = await me();
    if (current === null && DEMO_EMAIL && DEMO_PASSWORD) {
      try {
        await login(DEMO_EMAIL, DEMO_PASSWORD);
        current = await me();
      } catch {
        // Demo account not reachable/provisioned -- fall through to the normal login page
        // rather than masking a real problem.
      }
    }
    setUser(current);
  };

  useEffect(() => {
    // A plain statement inside a nested async function, not a .then()/.finally() chained
    // directly on a promise in the effect body -- the latter is what
    // react-hooks/set-state-in-effect actually flags (cascading-render risk); this is the
    // standard escape hatch for the common "fetch on mount, then clear a loading flag" case.
    const load = async () => {
      await refresh();
      setLoading(false);
    };
    void load();
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, refresh }}>{children}</AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error("useAuth() must be used inside an AuthProvider");
  }
  return context;
}
