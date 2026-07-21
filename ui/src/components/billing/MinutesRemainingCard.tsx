"use client";

import { useEffect, useRef, useState } from "react";

import { getBalanceApiV1BillingBalanceGet } from "@/client/sdk.gen";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth";

/**
 * SaaS-mode-only card showing the organization's remaining call minutes,
 * derived from the platform-managed credit balance. Not rendered in OSS
 * mode — callers must gate on `deploymentMode === 'saas'`.
 */
export function MinutesRemainingCard() {
  const { user, loading: authLoading } = useAuth();
  const [minutes, setMinutes] = useState<number | null>(null);
  const hasFetched = useRef(false);

  useEffect(() => {
    if (authLoading || !user || hasFetched.current) return;
    hasFetched.current = true;

    getBalanceApiV1BillingBalanceGet().then((response) => {
      if (response.error) {
        console.error("Failed to fetch billing balance:", response.error);
        return;
      }
      if (response.data) {
        setMinutes(response.data.minutes_equivalent);
      }
    });
  }, [authLoading, user]);

  if (minutes === null) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Call minutes remaining</CardTitle>
      </CardHeader>
      <CardContent>
        <span className="text-3xl font-semibold">{minutes}</span>
        <span className="ml-1 text-muted-foreground">min at standard rate</span>
      </CardContent>
    </Card>
  );
}
