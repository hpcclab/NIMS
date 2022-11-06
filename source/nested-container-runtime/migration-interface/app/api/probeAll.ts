import {FastifyReply, FastifyRequest} from "fastify"
import {inspectContainer, listContainer} from "../docker"
import {CreateRequestType} from "../schema"

async function probeAll(request: FastifyRequest<{ Params: CreateRequestType }>, reply: FastifyReply) {
    const containerInfos: any[] = await listContainer(request.log, {all: true})

    const states = await Promise.all(containerInfos.map(containerInfo => inspectContainer(containerInfo.Id, request.log)))

    let created = false
    let running = false

    for (const state of states) {
        const {State} = state
        const {Status, ExitCode} = State
        if (Status === "exited" || Status === "dead") {
            reply.code(503)
            return ExitCode || 1
        } else if (Status === "created") {
            created = true
        } else {
            running = true
        }
    }

    if (created && running) {
        reply.code(201)
        return ""
    } else if (created) {
        reply.code(204)
        return
    } else {
        return ""
    }
}

export {probeAll}