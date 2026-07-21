'use client';

import { ClerkProvider, useAuth as useClerkAuth, useClerk, useUser } from '@clerk/nextjs';
import React, { useMemo } from 'react';

import type { AuthUser } from '../types';
import { AuthContext } from './AuthProvider';

function ClerkAuthBridge({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useClerkAuth();
  const { user } = useUser();
  const clerk = useClerk();

  const contextValue = useMemo(
    () => ({
      user: (user
        ? {
            id: user.id,
            email: user.primaryEmailAddress?.emailAddress ?? '',
            name: user.fullName ?? undefined,
            displayName: user.fullName ?? undefined,
            image: user.imageUrl ?? undefined,
            provider: 'clerk' as const,
          }
        : null) as AuthUser | null,
      isAuthenticated: !!isSignedIn,
      loading: !isLoaded,
      getAccessToken: async () => (await getToken()) ?? '',
      redirectToLogin: () => {
        window.location.href = '/auth/login';
      },
      logout: async () => {
        await clerk.signOut();
        window.location.href = '/auth/login';
      },
      provider: 'clerk' as const,
    }),
    [user, isSignedIn, isLoaded, getToken, clerk],
  );

  return <AuthContext.Provider value={contextValue}>{children}</AuthContext.Provider>;
}

export function ClerkProviderWrapper({ children }: { children: React.ReactNode }) {
  return (
    <ClerkProvider signInUrl="/auth/login" signUpUrl="/auth/signup">
      <ClerkAuthBridge>{children}</ClerkAuthBridge>
    </ClerkProvider>
  );
}
