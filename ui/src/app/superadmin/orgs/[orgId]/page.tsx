"use client";

import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth";

interface OrgMemberDetail {
    user_id: number;
    email: string | null;
    role: "admin" | "member";
    created_at: string;
}

interface OrgDetail {
    id: number;
    provider_id: string;
    credit_balance_cents: number;
    members: OrgMemberDetail[];
}

export default function SuperadminOrgDetailPage() {
    const { user, getAccessToken } = useAuth();
    const params = useParams();
    const orgId = params?.orgId as string;
    const [org, setOrg] = useState<OrgDetail | null>(null);
    const [error, setError] = useState("");
    const hasFetched = useRef(false);

    const fetchOrg = async () => {
        const token = await getAccessToken();
        const res = await fetch(`/api/v1/superuser/orgs/${orgId}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
            setError("Failed to load organization");
            return;
        }
        setOrg(await res.json());
    };

    useEffect(() => {
        if (!user || !orgId || hasFetched.current) return;
        hasFetched.current = true;
        fetchOrg();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [user, orgId]);

    const overrideRole = async (userId: number, role: "admin" | "member") => {
        setError("");
        const token = await getAccessToken();
        const res = await fetch(
            `/api/v1/superuser/orgs/${orgId}/members/${userId}/role`,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Authorization: `Bearer ${token}`,
                },
                body: JSON.stringify({ role }),
            }
        );
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            setError(
                typeof body?.detail === "string" ? body.detail : "Failed to set role"
            );
            return;
        }
        await fetchOrg();
    };

    if (!org) {
        return (
            <main className="container mx-auto p-6 max-w-3xl">
                {error ? (
                    <p className="text-sm text-destructive">{error}</p>
                ) : (
                    <p className="text-muted-foreground">Loading…</p>
                )}
            </main>
        );
    }

    return (
        <main className="container mx-auto p-6 space-y-6 max-w-3xl">
            <div>
                <h1 className="text-2xl font-bold">{org.provider_id}</h1>
                <p className="text-sm text-muted-foreground">
                    Balance: ${(org.credit_balance_cents / 100).toFixed(2)}
                </p>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Card>
                <CardHeader>
                    <CardTitle>Members</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                    {org.members.map((m) => (
                        <div
                            key={m.user_id}
                            className="flex items-center justify-between border-b py-2"
                        >
                            <span>{m.email ?? `User #${m.user_id}`}</span>
                            <select
                                className="border rounded-md h-8 px-2 text-sm"
                                value={m.role}
                                onChange={(e) =>
                                    overrideRole(
                                        m.user_id,
                                        e.target.value as "admin" | "member"
                                    )
                                }
                            >
                                <option value="member">Member</option>
                                <option value="admin">Admin</option>
                            </select>
                        </div>
                    ))}
                </CardContent>
            </Card>
        </main>
    );
}
