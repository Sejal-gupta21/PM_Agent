// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebApi } from "azure-devops-node-api";
import { z } from "zod";
import fetch from "node-fetch";
// @ts-ignore
import type {} from 'node-fetch';

export function configureTimeLogTools(server: McpServer, tokenProvider: () => Promise<string>, connectionProvider: () => Promise<WebApi>) {
  server.tool(
    "timelog_get_work_item_logs",
    "Get time tracking information and comments for a specified Azure DevOps work item including completed work hours, original estimate, remaining work, and comments from work item discussions.",
    {
      workItemId: z.string().describe("The work item ID to fetch time tracking information for."),
      includeComments: z.boolean().optional().describe("Whether to include work item comments. Default is false."),
      commentLimit: z.number().optional().describe("Maximum number of comments to return. Default is 10."),
      includeTimeLogExtension: z.boolean().optional().describe("Whether to fetch entries from the Time Log extension. Default is false."),
    },
    async ({ workItemId, includeComments = false, commentLimit = 10, includeTimeLogExtension = false }) => {
      try {
        const org = "Stratagen";
        
        // Get the PAT token from the token provider
        const token = await tokenProvider();
        const authHeader = `Basic ${Buffer.from(`:${token}`).toString('base64')}`;
        
        const headers = {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "Authorization": authHeader,
        };
        
        // Fetch work item with all fields including time tracking
        const workItemUrl = `https://dev.azure.com/${org}/_apis/wit/workitems/${workItemId}?$expand=all&api-version=7.1`;
        
        const response = await fetch(workItemUrl, { headers });

        if (!response.ok) {
          return {
            content: [{ type: "text", text: `Failed to fetch work item ${workItemId}: ${response.statusText}` }],
            isError: true,
          };
        }

        const workItem = await response.json();
        const fields = workItem.fields || {};
        
        // Extract time-related fields
        const completedWork = fields["Microsoft.VSTS.Scheduling.CompletedWork"] || 0;
        const originalEstimate = fields["Microsoft.VSTS.Scheduling.OriginalEstimate"] || "Not set";
        const remainingWork = fields["Microsoft.VSTS.Scheduling.RemainingWork"] || "Not set";
        const title = fields["System.Title"] || "No title";
        const state = fields["System.State"] || "Unknown";
        const assignedTo = fields["System.AssignedTo"]?.displayName || "Unassigned";
        const workItemType = fields["System.WorkItemType"] || "Unknown";
        const commentCount = fields["System.CommentCount"] || 0;
        
        // Extract deployment schedule fields
        const qaDeployDate = fields["Custom.QADeployDate"];
        const preProdDeployDate = fields["Custom.PreProdDeployDate"];
        const prodScheduledDeployment = fields["Custom.PRODScheduledDeployment"];
        const deployedTo = fields["Custom.DeployedTo"] || "Not deployed";
        
        // Format dates
        const formatDate = (dateStr: string | undefined) => {
          if (!dateStr) return "Not scheduled";
          try {
            const date = new Date(dateStr);
            return date.toLocaleString('en-US', { 
              year: 'numeric', 
              month: 'short', 
              day: 'numeric', 
              hour: '2-digit', 
              minute: '2-digit' 
            });
          } catch {
            return dateStr;
          }
        };
        
        // Build the response with clean formatting
        let result = `📊 TIME TRACKING FOR WORK ITEM #${workItemId}\n\n`;
        
        result += `WORK ITEM INFORMATION:\n`;
        result += `  Title:        ${title}\n`;
        result += `  Type:         ${workItemType}\n`;
        result += `  Status:       ${state}\n`;
        result += `  Assigned To:  ${assignedTo}\n`;
        result += `  Deployed To:  ${deployedTo}\n\n`;
        
        result += `TIME SUMMARY:\n`;
        result += `  Completed Work:     ${completedWork} hours\n`;
        result += `  Original Estimate:  ${originalEstimate === "Not set" ? "Not set" : originalEstimate + " hours"}\n`;
        result += `  Remaining Work:     ${remainingWork === "Not set" ? "Not set" : remainingWork + " hours"}\n`;
        
        // Add deployment schedule section
        result += `\nDEPLOYMENT SCHEDULE:\n`;
        result += `  QA Deploy Date:         ${formatDate(qaDeployDate)}\n`;
        result += `  Pre-Prod Deploy Date:   ${formatDate(preProdDeployDate)}\n`;
        result += `  Prod Scheduled Deploy:  ${formatDate(prodScheduledDeployment)}\n`;

        // Fetch comments if requested
        if (includeComments && commentCount > 0) {
          const commentsUrl = `https://dev.azure.com/${org}/_apis/wit/workitems/${workItemId}/comments?api-version=7.1-preview.3`;
          const commentsResponse = await fetch(commentsUrl, { headers });

          if (commentsResponse.ok) {
            const commentsData = await commentsResponse.json();
            const comments = commentsData.comments || [];
            
            result += `\n\nCOMMENTS (${comments.length} total):\n`;
            
            const limitedComments = comments.slice(0, commentLimit);
            
            for (let i = 0; i < limitedComments.length; i++) {
              const comment = limitedComments[i];
              const author = comment.createdBy?.displayName || "Unknown";
              const date = comment.createdDate ? new Date(comment.createdDate).toLocaleString() : "Unknown date";
              const text = comment.text || "";
              
              // Remove HTML tags for cleaner display
              const cleanText = text.replace(/<[^>]*>/g, '').trim();
              
              result += `\n  ${i + 1}. ${author} (${date}):\n`;
              result += `     ${cleanText.substring(0, 200)}${cleanText.length > 200 ? '...' : ''}\n`;
            }
            
            if (comments.length > commentLimit) {
              result += `\n  ... and ${comments.length - commentLimit} more comments\n`;
            }
          } else {
            result += `\n\nCOMMENTS: Unable to fetch (${commentsResponse.statusText})\n`;
          }
        } else if (includeComments && commentCount === 0) {
          result += `\n\nCOMMENTS: No comments found\n`;
        }

        // If requested, try to fetch Time Log extension entries
        if (includeTimeLogExtension) {
          try {
            const apiVersion = "3.1-preview.1";
            const baseUrl = `https://extmgmt.dev.azure.com/${org}/_apis/ExtensionManagement/InstalledExtensions/TimeLog/time-logging-extension/Data/Scopes/Default/Current`;

            // List collections
            const collectionsUrl = `${baseUrl}/Collections?api-version=${apiVersion}`;
            const collectionsResp = await fetch(collectionsUrl, { headers });
            if (collectionsResp.ok) {
              const collectionsData = await collectionsResp.json();
              const collections = collectionsData.value || [];
              const allEntries: Array<any> = [];

              for (const col of collections) {
                const collectionId = col.id || col.name || col;
                const docsUrl = `${baseUrl}/Collections/${collectionId}/Documents?api-version=${apiVersion}`;
                const docsResp = await fetch(docsUrl, { headers });
                if (!docsResp.ok) continue;
                const docsData = await docsResp.json();
                const docs = docsData.value || [];

                for (const d of docs) {
                  const docId = d.id || d.name || d;
                  const docUrl = `${baseUrl}/Collections/${collectionId}/Documents/${docId}?api-version=${apiVersion}`;
                  const docResp = await fetch(docUrl, { headers });
                  if (!docResp.ok) continue;
                  const docData = await docResp.json();

                  // docData may contain nested arrays or objects with entries
                  const candidates: any[] = [];
                  if (Array.isArray(docData)) candidates.push(...docData);
                  else if (docData.items && Array.isArray(docData.items)) candidates.push(...docData.items);
                  else candidates.push(docData);

                  for (const c of candidates) {
                    if (Array.isArray(c)) {
                      for (const e of c) allEntries.push(e);
                    } else if (c && typeof c === 'object') {
                      if (c.workItemId || c.WorkItemId || c.itemId || c.id) {
                        allEntries.push(c);
                      } else {
                        for (const v of Object.values(c)) {
                          if (Array.isArray(v)) allEntries.push(...v);
                        }
                      }
                    }
                  }
                }
              }

              // Filter entries for this work item
              const wiStr = String(workItemId);
              const filtered = allEntries.filter((entry: any) => {
                const wid = entry.workItemId || entry.WorkItemId || entry.workItem || entry.itemId || entry.id || entry.parentId;
                if (!wid) return false;
                return String(wid).includes(wiStr);
              });

              if (filtered.length) {
                const timeLogs = filtered.map((e: any) => {
                  const userObj = e.user || e.author || e.createdBy || e.creator || e.createdBy?.user;
                  const user = typeof userObj === 'string' ? userObj : (userObj?.displayName || userObj?.uniqueName || userObj?.name || 'Unknown');
                  const email = e.email || e.createdBy?.uniqueName || e.createdBy?.email || '';
                  const date = e.date || e.logDate || e.created || e.timestamp || e.createdDate || null;
                  const hours = (typeof e.hours === 'number' && e.hours) ? e.hours : ((typeof e.timeSpent === 'number' && e.timeSpent) ? e.timeSpent : (typeof e.duration === 'number' ? e.duration : (e.hoursString ? parseFloat(e.hoursString) : 0)));
                  const comment = e.comment || e.notes || e.description || e.message || e.text || '';
                  return {
                    user: String(user),
                    email: String(email || ''),
                    date: date ? new Date(date).toISOString() : null,
                    hours: Number(hours) || 0,
                    comment: String(comment || ''),
                  };
                });

                return {
                  content: [{ type: 'text', text: JSON.stringify({ workItemId: Number(workItemId), timeLogs }) }],
                };
              }
            } else {
              // Auth or other error; let fallback happen below
              try {
                const txt = await collectionsResp.text();
                console.warn(`TimeLog extension collections returned ${collectionsResp.status}: ${txt}`);
              } catch {
                console.warn(`TimeLog extension collections returned ${collectionsResp.status}`);
              }
            }
          } catch (extErr) {
            console.warn('Error fetching TimeLog extension data', extErr);
          }
        }

        // If extension entries were not returned, fall back to revisions-based attribution
        try {
          const updatesUrl = `https://dev.azure.com/${org}/_apis/wit/workitems/${workItemId}/updates?api-version=7.1`;
          const updatesResp = await fetch(updatesUrl, { headers });
          if (updatesResp.ok) {
            const updatesData = await updatesResp.json();
            const updates = updatesData.value || [];
            const timeLogsFromRevisions: Array<any> = [];

            for (const upd of updates) {
              const revFields = upd.fields || {};
              const cwChange = revFields["Microsoft.VSTS.Scheduling.CompletedWork"];
              if (cwChange && (cwChange.oldValue !== undefined || cwChange.newValue !== undefined)) {
                const oldVal = Number(cwChange.oldValue || 0);
                const newVal = Number(cwChange.newValue || 0);
                const delta = newVal - oldVal;
                if (Math.abs(delta) > 0.0001) {
                  const author = upd.revisedBy?.displayName || upd.revisedBy?.uniqueName || (upd.createdBy && (upd.createdBy.displayName || upd.createdBy.uniqueName)) || 'Unknown';
                  const email = upd.revisedBy?.uniqueName || upd.revisedBy?.email || '';
                  const date = upd.revisedDate || upd.createdDate || upd.fields?.["System.ChangedDate"] || null;
                  // Try to get a comment/description associated with this update
                  let commentText = '';
                  if (revFields["System.History"] && revFields["System.History"].newValue) {
                    commentText = String(revFields["System.History"].newValue || '');
                  } else if (upd.comment && upd.comment.text) {
                    commentText = upd.comment.text;
                  }

                  timeLogsFromRevisions.push({
                    user: String(author),
                    email: String(email || ''),
                    date: date ? new Date(date).toISOString() : null,
                    hours: Number(delta) || 0,
                    comment: String(commentText || ''),
                  });
                }
              }
            }

            if (timeLogsFromRevisions.length) {
              return {
                content: [{ type: 'text', text: JSON.stringify({ workItemId: Number(workItemId), timeLogs: timeLogsFromRevisions }) }],
              };
            }
          }
        } catch (revErr) {
          console.warn('Error fetching work item updates for revisions-based time attribution', revErr);
        }

        // Final fallback: return human-friendly text result
        return {
          content: [{ type: 'text', text: result }],
        };
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
        return {
          content: [{ type: "text", text: `Error fetching time tracking information: ${errorMessage}` }],
          isError: true,
        };
      }
    }
  );
}
