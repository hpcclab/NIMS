import {FastifyReply, FastifyRequest} from "fastify"
import {checkpointContainer, ContainerInfo, inspectContainer, listContainer} from "../docker"
import {MigrateRequestType} from "../schema"
import {execRsync, findDestinationFileSystemId, waitForIt} from "../lib"
import {FastifyLoggerInstance} from "fastify/types/logger"
import dotenv from "dotenv"
import {readFileSync} from "fs"
import chokidar from "chokidar"
import {AsyncBlockingQueue} from "../queue"


async function migrate(request: FastifyRequest<{ Body: MigrateRequestType }>, reply: FastifyReply) {
    const containerInfos: any[] = await listContainer(request.log)

    const {checkpointId, interfaceHost, interfacePort, containers, volumes} = request.body

    const config = dotenv.parse(readFileSync('/etc/podinfo/annotations', 'utf8'))
    const exit = config[process.env.START_MODE_ANNOTATION!] !== process.env.START_MODE_ACTIVE

    await waitForIt(interfaceHost, interfacePort, request.log)

    const responses = await Promise.all([
        ...containerInfos.map(
            containerInfo => migrateOneContainer(checkpointId, interfaceHost, interfacePort,
                containers, containerInfo, exit, request.log)
        ),
        ...volumes.map(
            volume => transferVolume(interfaceHost, interfacePort, volume, request.log)
        )
    ])
    request.log.info(responses)
    reply.code(204)
}

async function migrateOneContainer(checkpointId: string, interfaceHost: string, interfacePort: string, containers: any,
                                   containerInfo: ContainerInfo, exit: boolean, log: FastifyLoggerInstance) {
    const {destinationId, destinationFs} = findDestinationFileSystemId(containers, containerInfo)

    const sourceImagePath = `/var/lib/docker/containers/${containerInfo.Id}/checkpoints/${checkpointId}`
    const destinationImagePath = `root@${interfaceHost}:/var/lib/docker/containers/${destinationId}/checkpoints`
    const imageQueue = new AsyncBlockingQueue<string>()
    let imageQueueInit: (value: unknown) => void
    const imageQueueInitPromise = new Promise(resolve => {
        imageQueueInit = resolve
    })
    const imageWatcher = chokidar.watch(sourceImagePath)
    imageWatcher
        .on('all', (event, path) => {
            if ((event === 'add' || event == 'change') && imageQueue.isEmpty()) {
                imageQueue.enqueue(path)
            }
        })
        .on('ready', () => {
            imageQueueInit(null)
        })

    await imageQueueInitPromise

    await Promise.all([
        transferContainerImage(interfacePort, imageQueue, sourceImagePath, destinationImagePath, log),
        transferContainerFS(interfaceHost, interfacePort, containerInfo, destinationFs, log),
        checkpointContainer(containerInfo.Id, checkpointId, exit, imageQueue, log)
    ])

    await imageWatcher.close()
}

async function transferContainerImage(interfacePort: string, queue: AsyncBlockingQueue<string>, sourcePath: string,
                                      destinationPath: string, log: FastifyLoggerInstance) {
    while (true) {
        if (queue.done) break
        await queue.dequeue()
        await execRsync(interfacePort, sourcePath, destinationPath, log)
    }

    while (!queue.isEmpty()) {
        await queue.dequeue()
    }

    await execRsync(interfacePort, sourcePath, destinationPath, log)
}

async function transferContainerFS(interfaceHost: string, interfacePort: string, containerInfo: ContainerInfo,
                                   destinationFs: string, log: FastifyLoggerInstance) {
    const {GraphDriver: {Name, Data: {UpperDir}}} = await inspectContainer(containerInfo.Id, log)
    if (Name === 'overlay2' && destinationFs !== null) {
        await execRsync(interfacePort, UpperDir, `root@${interfaceHost}:${destinationFs}`.slice(0, -5), log)
    }
}

async function transferVolume(interfaceHost: string, interfacePort: string, volume: any, log: FastifyLoggerInstance) {
    await execRsync(interfacePort, volume, `root@${interfaceHost}:/mount`, log)
}

export {migrate}