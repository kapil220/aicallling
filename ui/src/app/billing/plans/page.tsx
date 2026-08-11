"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
    changePlanApiV1BillingChangePlanPost,
    getSubscriptionApiV1BillingSubscriptionGet,
    listPlansApiV1BillingPlansGet,
    subscribeApiV1BillingSubscribePost,
} from "@/client/sdk.gen";
import type { PlanPublicResponse } from "@/client/types.gen";
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
import { useAppConfig } from "@/context/AppConfigContext";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

function priceLabel(cents: number, currency: string) {
    const amount = (cents / 100).toLocaleString("en-IN");
    return currency === "inr" ? `₹${amount}` : `${currency.toUpperCase()} ${amount}`;
}

function limitLabel(value: number | null | undefined, noun: string) {
    return value == null ? `Unlimited ${noun}` : `${value} ${noun}`;
}

export default function PlansPage() {
    const { config, loading: configLoading } = useAppConfig();
    const { user, loading: authLoading } = useAuth();
    const router = useRouter();
    const [plans, setPlans] = useState<PlanPublicResponse[]>([]);
    const [hasActiveSub, setHasActiveSub] = useState(false);
    const [loading, setLoading] = useState(true);
    const [busyTier, setBusyTier] = useState<string | null>(null);
    const hasFetched = useRef(false);

    useEffect(() => {
        if (configLoading || authLoading || !user || hasFetched.current) return;
        if (config?.deploymentMode !== "saas") {
            router.replace("/billing");
            return;
        }
        hasFetched.current = true;
        Promise.all([
            listPlansApiV1BillingPlansGet(),
            getSubscriptionApiV1BillingSubscriptionGet(),
        ]).then(([plansResponse, subscriptionResponse]) => {
            if (!plansResponse.error && plansResponse.data) {
                setPlans(plansResponse.data);
            }
            setHasActiveSub(
                subscriptionResponse.data?.subscription_status === "active",
            );
            setLoading(false);
        });
    }, [configLoading, authLoading, user, config?.deploymentMode, router]);

    const choosePlan = async (tierKey: string) => {
        setBusyTier(tierKey);
        const call = hasActiveSub
            ? changePlanApiV1BillingChangePlanPost
            : subscribeApiV1BillingSubscribePost;
        const response = await call({ body: { tier_key: tierKey } });
        setBusyTier(null);
        if (response.error || !response.data) {
            toast.error(detailFromError(response.error, "Could not start checkout."));
            return;
        }
        window.location.href = response.data.checkout_url;
    };

    if (loading) {
        return (
            <div className="container mx-auto p-6 space-y-6">
                <Skeleton className="h-9 w-64" />
                <div className="grid gap-6 md:grid-cols-3">
                    <Skeleton className="h-80 rounded-lg" />
                    <Skeleton className="h-80 rounded-lg" />
                    <Skeleton className="h-80 rounded-lg" />
                </div>
            </div>
        );
    }

    return (
        <div className="container mx-auto max-w-5xl p-6">
            <h1 className="mb-2 text-3xl font-bold">Choose your plan</h1>
            <p className="mb-8 text-muted-foreground">
                Every plan includes monthly calling minutes. Model choice can burn
                minutes faster — the multiplier is shown in the agent builder.
            </p>
            <div className="grid gap-6 md:grid-cols-3">
                {plans.map((plan) => (
                    <Card
                        key={plan.tier_key}
                        className={plan.is_current ? "border-primary" : ""}
                    >
                        <CardHeader>
                            <CardTitle className="flex items-center justify-between">
                                {plan.display_name}
                                {plan.is_current && <Badge>Current</Badge>}
                            </CardTitle>
                            <CardDescription>
                                <span className="text-2xl font-semibold text-foreground">
                                    {priceLabel(plan.price_cents, plan.currency)}
                                </span>
                                /month
                            </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-2 text-sm">
                            <div>{plan.included_minutes.toLocaleString()} minutes / month</div>
                            <div>{limitLabel(plan.max_agents, "agents")}</div>
                            <div>{plan.max_concurrent_calls} concurrent calls</div>
                            <div>{limitLabel(plan.max_active_campaigns, "active campaigns")}</div>
                            <Button
                                className="mt-4 w-full"
                                disabled={plan.is_current || busyTier !== null}
                                onClick={() => choosePlan(plan.tier_key)}
                            >
                                {busyTier === plan.tier_key
                                    ? "Starting checkout..."
                                    : plan.is_current
                                        ? "Your plan"
                                        : hasActiveSub
                                            ? "Switch to this plan"
                                            : "Subscribe"}
                            </Button>
                        </CardContent>
                    </Card>
                ))}
            </div>
        </div>
    );
}
