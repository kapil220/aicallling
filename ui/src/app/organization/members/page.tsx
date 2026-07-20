"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/lib/auth";

interface Member {
    user_id: number;
    email: string | null;
    role: "admin" | "member";
    created_at: string;
}

export default function MembersPage() {
    const { user, getAccessToken } = useAuth();
    const [members, setMembers] = useState<Member[]>([]);
    const [inviteEmail, setInviteEmail] = useState("");
    const [inviteRole, setInviteRole] = useState<"admin" | "member">("member");
    const [error, setError] = useState("");
    const hasFetched = useRef(false);

    // Derive the caller's role from the roster (no shared auth-context change).
    const selfRole =
        members.find((m) => String(m.user_id) === String(user?.id))?.role ?? null;
    const isAdmin = selfRole === "admin";

    const fetchMembers = async () => {
        const token = await getAccessToken();
        const res = await fetch("/api/v1/organization/members", {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
            setError("Failed to load members");
            return;
        }
        setMembers(await res.json());
    };

    useEffect(() => {
        if (!user || hasFetched.current) return;
        hasFetched.current = true;
        fetchMembers();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [user]);

    const detail = async (res: Response, fallback: string) => {
        const body = await res.json().catch(() => ({}));
        return typeof body?.detail === "string" ? body.detail : fallback;
    };

    const handleInvite = async (e: React.FormEvent) => {
        e.preventDefault();
        setError("");
        const token = await getAccessToken();
        const res = await fetch("/api/v1/organization/members/invite", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ email: inviteEmail, role: inviteRole }),
        });
        if (!res.ok) {
            setError(await detail(res, "Failed to invite member"));
            return;
        }
        setInviteEmail("");
        await fetchMembers();
    };

    const handleRoleChange = async (userId: number, role: "admin" | "member") => {
        setError("");
        const token = await getAccessToken();
        const res = await fetch(`/api/v1/organization/members/${userId}`, {
            method: "PATCH",
            headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ role }),
        });
        if (!res.ok) {
            setError(await detail(res, "Failed to change role"));
            return;
        }
        await fetchMembers();
    };

    const handleRemove = async (userId: number) => {
        setError("");
        const token = await getAccessToken();
        const res = await fetch(`/api/v1/organization/members/${userId}`, {
            method: "DELETE",
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
            setError(await detail(res, "Failed to remove member"));
            return;
        }
        await fetchMembers();
    };

    return (
        <main className="container mx-auto p-6 space-y-6 max-w-3xl">
            <h1 className="text-2xl font-bold">Members</h1>
            {error && <p className="text-sm text-destructive">{error}</p>}
            {!isAdmin && (
                <p className="text-sm text-muted-foreground">
                    You have read-only access. Member management is available to org
                    admins.
                </p>
            )}

            {isAdmin && (
                <Card>
                    <CardHeader>
                        <CardTitle>Invite a member</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <form onSubmit={handleInvite} className="flex gap-2 items-end">
                            <div className="space-y-2 flex-1">
                                <Label htmlFor="invite-email">Email</Label>
                                <Input
                                    id="invite-email"
                                    type="email"
                                    value={inviteEmail}
                                    onChange={(e) => setInviteEmail(e.target.value)}
                                    required
                                />
                            </div>
                            <div className="space-y-2">
                                <Label htmlFor="invite-role">Role</Label>
                                <select
                                    id="invite-role"
                                    className="border rounded-md h-9 px-2"
                                    value={inviteRole}
                                    onChange={(e) =>
                                        setInviteRole(
                                            e.target.value as "admin" | "member"
                                        )
                                    }
                                >
                                    <option value="member">Member</option>
                                    <option value="admin">Admin</option>
                                </select>
                            </div>
                            <Button type="submit">Invite</Button>
                        </form>
                    </CardContent>
                </Card>
            )}

            <Card>
                <CardHeader>
                    <CardTitle>Roster</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                    {members.map((m) => (
                        <div
                            key={m.user_id}
                            className="flex items-center justify-between border-b py-2"
                        >
                            <div>
                                <p className="font-medium">
                                    {m.email ?? `User #${m.user_id}`}
                                </p>
                                <p className="text-xs text-muted-foreground">
                                    Member since{" "}
                                    {new Date(m.created_at).toLocaleDateString()}
                                </p>
                            </div>
                            {isAdmin && (
                                <div className="flex items-center gap-2">
                                    <select
                                        className="border rounded-md h-8 px-2 text-sm"
                                        value={m.role}
                                        onChange={(e) =>
                                            handleRoleChange(
                                                m.user_id,
                                                e.target.value as "admin" | "member"
                                            )
                                        }
                                        disabled={m.user_id === Number(user?.id)}
                                    >
                                        <option value="member">Member</option>
                                        <option value="admin">Admin</option>
                                    </select>
                                    <Button
                                        variant="destructive"
                                        size="sm"
                                        onClick={() => handleRemove(m.user_id)}
                                        disabled={m.user_id === Number(user?.id)}
                                    >
                                        Remove
                                    </Button>
                                </div>
                            )}
                            {!isAdmin && (
                                <span className="text-sm text-muted-foreground capitalize">
                                    {m.role}
                                </span>
                            )}
                        </div>
                    ))}
                </CardContent>
            </Card>
        </main>
    );
}
