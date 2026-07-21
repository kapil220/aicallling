"use client";

import { UserProfile } from "@clerk/nextjs";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
    getWorkspaceProfileApiV1UserWorkspaceProfileGet,
    putWorkspaceProfileApiV1UserWorkspaceProfilePut,
} from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

const TIMEZONES: string[] =
    typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];

export default function ProfilePage() {
    const { user, loading: authLoading, provider } = useAuth();
    const hasFetched = useRef(false);

    const [companyName, setCompanyName] = useState("");
    const [timezone, setTimezone] = useState<string>("");
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const fetchWorkspaceProfile = useCallback(async () => {
        try {
            setLoading(true);
            setError(null);
            const response = await getWorkspaceProfileApiV1UserWorkspaceProfileGet();
            if (response.error) {
                setError(detailFromError(response.error, "Failed to load workspace profile"));
                return;
            }
            setCompanyName(response.data?.company_name ?? "");
            setTimezone(response.data?.timezone ?? "");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        fetchWorkspaceProfile();
    }, [authLoading, user, fetchWorkspaceProfile]);

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        try {
            const response = await putWorkspaceProfileApiV1UserWorkspaceProfilePut({
                body: {
                    company_name: companyName || null,
                    timezone: timezone || null,
                },
            });
            if (response.error) {
                setError(detailFromError(response.error, "Failed to save workspace profile"));
                return;
            }
            toast.success("Workspace profile saved");
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="container mx-auto max-w-3xl space-y-6 p-6">
            <div>
                <h1 className="text-3xl font-bold mb-2">Profile</h1>
                <p className="text-muted-foreground">
                    Manage your account and workspace settings.
                </p>
            </div>

            {provider === "clerk" && (
                <div className="flex justify-center">
                    <UserProfile routing="hash" />
                </div>
            )}

            <Card>
                <CardHeader>
                    <CardTitle>Workspace</CardTitle>
                    <CardDescription>
                        Company details and timezone used across your workspace.
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    {loading ? (
                        <div className="space-y-4">
                            <Skeleton className="h-9 w-full" />
                            <Skeleton className="h-9 w-full" />
                        </div>
                    ) : (
                        <>
                            {error && (
                                <p className="text-sm text-destructive whitespace-pre-line">{error}</p>
                            )}
                            <div className="space-y-2">
                                <Label htmlFor="company-name">Company name</Label>
                                <Input
                                    id="company-name"
                                    value={companyName}
                                    onChange={(e) => setCompanyName(e.target.value)}
                                    placeholder="Acme Inc."
                                />
                            </div>
                            <div className="space-y-2">
                                <Label htmlFor="timezone">Timezone</Label>
                                <Select value={timezone || undefined} onValueChange={setTimezone}>
                                    <SelectTrigger id="timezone" className="w-full">
                                        <SelectValue placeholder="Select a timezone" />
                                    </SelectTrigger>
                                    <SelectContent>
                                        {TIMEZONES.map((tz) => (
                                            <SelectItem key={tz} value={tz}>
                                                {tz}
                                            </SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>
                            <div className="flex justify-end">
                                <Button onClick={handleSave} disabled={saving}>
                                    {saving ? "Saving..." : "Save"}
                                </Button>
                            </div>
                        </>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}
