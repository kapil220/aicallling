"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
    cancelSubscriptionApiV1BillingCancelPost,
    getSubscriptionApiV1BillingSubscriptionGet,
} from "@/client/sdk.gen";
import type { SubscriptionResponse } from "@/client/types.gen";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

const STATUS_VARIANT: Record<string, "default" | "destructive" | "secondary"> = {
    active: "default",
    halted: "destructive",
    cancelled: "secondary",
};

export function CurrentPlanCard() {
    const { user, loading: authLoading } = useAuth();
    const [subscription, setSubscription] = useState<SubscriptionResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [cancelling, setCancelling] = useState(false);
    const hasFetched = useRef(false);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        getSubscriptionApiV1BillingSubscriptionGet().then((response) => {
            if (!response.error && response.data) {
                setSubscription(response.data);
            }
            setLoading(false);
        });
    }, [authLoading, user]);

    const handleCancel = async () => {
        setCancelling(true);
        const response = await cancelSubscriptionApiV1BillingCancelPost();
        setCancelling(false);
        if (response.error) {
            toast.error(detailFromError(response.error, "Could not cancel the subscription."));
            return;
        }
        toast.success("Cancellation scheduled for the end of the billing period.");
    };

    if (loading) {
        return <Skeleton className="h-40 w-full rounded-lg" />;
    }

    const status = subscription?.subscription_status;
    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    {subscription?.plan_display_name ?? "Free trial"}
                    {status && (
                        <Badge variant={STATUS_VARIANT[status] ?? "secondary"}>{status}</Badge>
                    )}
                </CardTitle>
                <CardDescription>
                    {subscription?.included_minutes != null
                        ? `${subscription.included_minutes.toLocaleString()} minutes included each month`
                        : "Trial minutes only — pick a plan to keep calling"}
                </CardDescription>
            </CardHeader>
            <CardContent className="flex items-center justify-between">
                <div className="text-sm text-muted-foreground">
                    {subscription?.current_period_end
                        ? `Renews ${new Date(subscription.current_period_end).toLocaleDateString()}`
                        : "No renewal scheduled"}
                </div>
                <div className="flex gap-2">
                    <Button asChild variant="outline">
                        <Link href="/billing/plans">
                            {status === "active" ? "Change plan" : "View plans"}
                        </Link>
                    </Button>
                    {status === "active" && (
                        <Button variant="ghost" onClick={handleCancel} disabled={cancelling}>
                            {cancelling ? "Cancelling..." : "Cancel"}
                        </Button>
                    )}
                </div>
            </CardContent>
        </Card>
    );
}
