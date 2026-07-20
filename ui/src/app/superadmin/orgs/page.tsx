"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth";

interface OrgSummary {
    id: number;
    provider_id: string;
    credit_balance_cents: number;
    member_count: number;
    admin_count: number;
}

export default function SuperadminOrgsPage() {
    const { user, getAccessToken } = useAuth();
    const [orgs, setOrgs] = useState<OrgSummary[]>([]);
    const [error, setError] = useState("");
    const hasFetched = useRef(false);

    useEffect(() => {
        if (!user || hasFetched.current) return;
        hasFetched.current = true;
        (async () => {
            const token = await getAccessToken();
            const res = await fetch("/api/v1/superuser/orgs?limit=200", {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (!res.ok) {
                setError("Failed to load organizations");
                return;
            }
            const body = await res.json();
            setOrgs(body.organizations ?? []);
        })();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [user]);

    return (
        <main className="container mx-auto p-6 space-y-6 max-w-4xl">
            <h1 className="text-2xl font-bold">Organizations</h1>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Card>
                <CardHeader>
                    <CardTitle>All organizations</CardTitle>
                </CardHeader>
                <CardContent>
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-muted-foreground border-b">
                                    <th className="py-2">Org</th>
                                    <th className="py-2">Balance</th>
                                    <th className="py-2">Members</th>
                                    <th className="py-2">Admins</th>
                                </tr>
                            </thead>
                            <tbody>
                                {orgs.map((o) => (
                                    <tr key={o.id} className="border-b">
                                        <td className="py-2">
                                            <Link
                                                className="text-primary underline"
                                                href={`/superadmin/orgs/${o.id}`}
                                            >
                                                {o.provider_id}
                                            </Link>
                                        </td>
                                        <td className="py-2">
                                            ${(o.credit_balance_cents / 100).toFixed(2)}
                                        </td>
                                        <td className="py-2">{o.member_count}</td>
                                        <td className="py-2">{o.admin_count}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </CardContent>
            </Card>
        </main>
    );
}
